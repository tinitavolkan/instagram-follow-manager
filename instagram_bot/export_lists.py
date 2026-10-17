"""Read Instagram's downloaded relationship files without network or extraction."""

from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urljoin, urlsplit
from zipfile import BadZipFile, ZipFile


class ExportImportError(ValueError):
    """The selected files cannot establish two consistent exported lists."""


@dataclass(frozen=True)
class ExportLists:
    followers: tuple[str, ...]
    following: tuple[str, ...]
    sources: tuple[str, ...]
    account_hint: str = ""
    warnings: tuple[str, ...] = ()
    fingerprint: str = ""


_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 50000
_RESERVED = {"accounts", "about", "api", "challenge", "checkpoint", "direct", "explore",
             "legal", "p", "privacy", "reel", "reels", "stories", "terms", "web", "_u"}
_LIST_FILE = re.compile(r"^(following|followers(?:_([0-9]+))?)\.(json|html|htm)$", re.I)
_ACCOUNT_FOLDER = re.compile(r"^instagram-([a-z0-9._]{1,30})-\d{4}-\d{2}-\d{2}(?:-.+)?$", re.I)
_HEADINGS = {"followers", "following", "takipçiler", "takipçi", "takip", "takip edilenler"}
_LIST_LABELS = {"followers": {"followers", "takipçiler", "takipçi"},
                "following": {"following", "takip", "takip edilenler"}}
_WARNINGS = (
    "Dosyalardaki tarih aralığı kesin olarak doğrulanamadı; tüm zamanları içeren indirmeyi seçtiğinden emin ol.",
    "Dosyalar son numaralı parçanın veya indirme kapsamının eksiksiz olduğunu tek başına doğrulamaz.",
)


def _name(value, *, url_only=False):
    if not isinstance(value, str) or not value.strip():
        raise ExportImportError("Boş veya metin olmayan kullanıcı adı bulundu.")
    value = value.strip()
    is_url = "://" in value or value.startswith(("/", "//"))
    if is_url:
        try:
            parsed = urlsplit(urljoin("https://www.instagram.com/", value))
            if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"instagram.com", "www.instagram.com"}
                    or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
                    or parsed.query or parsed.fragment):
                raise ValueError("not a plain Instagram profile URL")
            parts = unquote(parsed.path).strip("/").split("/")
            if len(parts) == 2 and parts[0] == "_u":
                parts = parts[1:]
            if len(parts) != 1:
                raise ValueError("not a profile path")
            value = parts[0]
        except (ValueError, UnicodeError) as exc:
            raise ExportImportError("Geçersiz Instagram profil bağlantısı bulundu.") from exc
    elif url_only:
        raise ExportImportError("Profil bağlantısı yerine beklenmeyen veri bulundu.")
    elif value.startswith("@"):
        value = value[1:]
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", value) or value.casefold() in _RESERVED:
        raise ExportImportError("Geçersiz kullanıcı adı bulundu.")
    return value.casefold()


def _classification(parts):
    if any(part.casefold() in {"threads", "your_threads_activity"} for part in parts[:-1]):
        return None
    match = _LIST_FILE.fullmatch(parts[-1]) if parts else None
    if not match:
        return None
    kind = "following" if match[1].casefold() == "following" else "followers"
    if match[2] and len(match[2]) > 6:
        raise ExportImportError("Takipçi parça numarası makul sınırları aşıyor.")
    index = int(match[2] or 1)
    if index < 1:
        raise ExportImportError("Takipçi parça numarası 1'den başlamalı.")
    return kind, index


def _hints(parts):
    found = set()
    for part in parts:
        stem = part[:-4] if part.casefold().endswith(".zip") else part
        match = _ACCOUNT_FOLDER.fullmatch(stem)
        if match:
            found.add(_name(match[1]))
    return found


def _json_names(raw, kind):
    document = json.loads(raw)
    if isinstance(document, dict):
        key = "relationships_" + kind
        if key not in document:
            raise ExportImportError("Dosyada beklenen ilişki listesi yok.")
        document = document[key]
    if not isinstance(document, list):
        raise ExportImportError("İlişki listesi bir JSON dizisi olmalı.")
    names = set()
    for row in document:
        if not isinstance(row, dict) or not isinstance(row.get("string_list_data"), list) or not row["string_list_data"]:
            raise ExportImportError("Bir kullanıcı satırının profil verisi eksik veya bozuk.")
        evidence = set()
        title = row.get("title")
        if "title" in row and not isinstance(title, str):
            raise ExportImportError("Bir kullanıcı satırının başlığı metin olmalı.")
        if title:
            evidence.add(_name(title))
        for item in row["string_list_data"]:
            if not isinstance(item, dict):
                raise ExportImportError("Bir kullanıcı satırı bozuk.")
            present = False
            for key in ("value", "href"):
                if key in item:
                    evidence.add(_name(item[key], url_only=key == "href"))
                    present = True
            if not present:
                raise ExportImportError("Bir kullanıcı satırında ad ve bağlantı eksik.")
        if len(evidence) != 1:
            raise ExportImportError("Aynı kullanıcı satırındaki ad ve bağlantı çelişiyor.")
        names.update(evidence)
    return names


@dataclass(eq=False)
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    parent: object = None
    children: list = field(default_factory=list)
    text: list = field(default_factory=list)

    def content(self):
        return " ".join(self.text + [child.content() for child in self.children]).strip()

    def descendants(self, tag):
        for child in self.children:
            if child.tag == tag:
                yield child
            yield from child.descendants(tag)


class _ExportHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 250000:
            raise ExportImportError("HTML dosyası işlenemeyecek kadar büyük.")
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not any(node.tag in {"script", "style"} for node in self.stack):
            self.stack[-1].text.append(data)


def _html_names(raw, kind):
    parser = _ExportHTML()
    parser.feed(raw)
    parser.close()
    declared = {" ".join(node.content().casefold().replace("\u0307", "").split())
                for tag in ("title", "h1") for node in parser.root.descendants(tag)}
    opposite = "following" if kind == "followers" else "followers"
    if declared.intersection(_LIST_LABELS[opposite]):
        raise ExportImportError("HTML başlığı ile seçilen takipçi/takip edilen dosya türü çelişiyor.")
    headings = []
    for heading in parser.root.descendants("h2"):
        text = heading.content()
        if text.casefold() not in _HEADINGS and text:
            headings.append((heading, _name(text)))
    names = set()
    heading_values = {id(node): value for node, value in headings}
    for anchor in parser.root.descendants("a"):
        href = anchor.attrs.get("href", "")
        if not href:
            continue
        local_headings = []
        parent = anchor.parent
        while parent and parent.tag not in {"body", "html", "main", "document"}:
            local_headings = [heading_values[id(node)] for node in parent.descendants("h2") if id(node) in heading_values]
            if local_headings:
                break
            parent = parent.parent
        parsed = urlsplit(urljoin("https://www.instagram.com/", href))
        if parsed.hostname not in {"instagram.com", "www.instagram.com"}:
            if local_headings:
                raise ExportImportError("Bir kullanıcı başlığının profil bağlantısı Instagram'a ait değil.")
            continue
        if parsed.path.strip("/").casefold() in _RESERVED or parsed.path in {"", "/"}:
            continue
        name = _name(href, url_only=True)
        text = anchor.content()
        if text and (re.fullmatch(r"@?[A-Za-z0-9._]{1,30}", text) or "://" in text or text.startswith("/")):
            if _name(text) != name:
                raise ExportImportError("HTML satırındaki kullanıcı adı ve bağlantı çelişiyor.")
        if len(local_headings) == 1 and local_headings[0] != name:
            raise ExportImportError("HTML başlığındaki kullanıcı adı ve bağlantı çelişiyor.")
        names.add(name)
    heading_names = {value for _, value in headings}
    if kind == "following" and heading_names:
        if names and names != heading_names:
            raise ExportImportError("HTML takip edilen başlıkları ve bağlantıları eksik veya çelişkili.")
        names.update(heading_names)
    if not names:
        titles = [node.content().casefold() for tag in ("title", "h1", "h2") for node in parser.root.descendants(tag)]
        labels = {"followers", "takipçiler", "takipçi"} if kind == "followers" else {"following", "takip", "takip edilenler"}
        if not labels.intersection(titles):
            raise ExportImportError("HTML dosyasında beklenen liste tanınamadı.")
    return names


def load_exports(paths) -> ExportLists:
    """Read selected JSON/HTML files, ZIPs, or folders; never alter their files."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    records = {}
    sources = set()
    account_hints = set()
    seen = set()
    total = 0

    def consume(source, parts, payload, classification):
        nonlocal total
        if source in seen:
            return
        seen.add(source)
        total += len(payload)
        if len(payload) > _MAX_FILE_BYTES or total > _MAX_TOTAL_BYTES:
            raise ExportImportError("Seçilen liste dosyaları boyut sınırını aşıyor.")
        account_hints.update(_hints(parts))
        try:
            text = payload.decode("utf-8-sig")
            names = _json_names(text, classification[0]) if parts[-1].casefold().endswith(".json") else _html_names(text, classification[0])
        except ExportImportError:
            raise
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ExportImportError(f"Liste dosyası okunamadı: {parts[-1]}") from exc
        if classification in records and records[classification] != names:
            raise ExportImportError("Aynı listenin veya parça numarasının birbiriyle çelişen kopyaları seçildi.")
        records[classification] = names
        sources.add(source)

    def read_file(path):
        if path.is_symlink():
            raise ExportImportError("Liste dosyası sembolik bağlantı olamaz.")
        classification = _classification(path.parts)
        if classification:
            with path.open("rb") as stream:
                consume(str(path.resolve()), path.parts, stream.read(_MAX_FILE_BYTES + 1), classification)

    try:
        for chosen in paths:
            path = Path(chosen)
            if not path.exists():
                raise ExportImportError("Seçilen dosya veya klasör bulunamadı.")
            account_hints.update(_hints(path.parts))
            if path.is_dir():
                for count, item in enumerate(path.rglob("*"), 1):
                    if count > 100000:
                        raise ExportImportError("Seçilen klasör çok fazla dosya içeriyor.")
                    if item.is_file() and _classification(item.parts):
                        read_file(item)
            elif path.suffix.casefold() == ".zip":
                if path.is_symlink():
                    raise ExportImportError("Arşiv sembolik bağlantı olamaz.")
                with ZipFile(path) as archive:
                    entries = archive.infolist()
                    if len(entries) > _MAX_ARCHIVE_ENTRIES:
                        raise ExportImportError("Arşiv çok fazla öğe içeriyor.")
                    for info in entries:
                        member = PurePosixPath(info.filename)
                        if member.is_absolute() or ".." in member.parts or "\\" in info.filename:
                            raise ExportImportError("Arşivde geçersiz dosya yolu bulundu.")
                        classification = _classification(member.parts)
                        if info.is_dir() or not classification:
                            continue
                        if info.file_size > _MAX_FILE_BYTES or info.flag_bits & 1:
                            raise ExportImportError("Liste dosyası çok büyük veya şifreli.")
                        with archive.open(info) as stream:
                            consume(f"{path.resolve()}!{info.filename}", path.parts + member.parts,
                                    stream.read(_MAX_FILE_BYTES + 1), classification)
            elif _classification(path.parts):
                read_file(path)
            else:
                raise ExportImportError("Takipçi/takip edilen JSON, HTML, ZIP veya klasörü seç.")
        if len(account_hints) > 1:
            raise ExportImportError("Seçilen kaynaklar farklı Instagram hesaplarına ait görünüyor.")
        indices = sorted(index for kind, index in records if kind == "followers")
        if not indices or ("following", 1) not in records:
            raise ExportImportError("Hem takipçi hem takip edilen listesi gerekli.")
        if indices != list(range(1, len(indices) + 1)):
            raise ExportImportError("Numaralı takipçi parçaları eksik; 1'den başlayıp kesintisiz seçilmeli.")
        followers = tuple(sorted(set().union(*(records[("followers", index)] for index in indices))))
        following = tuple(sorted(records[("following", 1)]))
        hint = next(iter(account_hints), "")
        fingerprint = sha256(json.dumps([followers, following], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        return ExportLists(followers, following, tuple(sorted(sources)), hint, _WARNINGS, fingerprint)
    except ExportImportError:
        raise
    except (OSError, BadZipFile, RuntimeError, TypeError) as exc:
        raise ExportImportError("Seçilen dışa aktarma dosyaları açılamadı.") from exc
