"""İndirilen Instagram listeleri ve tekil takibi bırakma uygulaması."""
import threading
from instagram_bot.paths import BASE_DIR
from instagram_bot.single_instance import SingleInstance

def main():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    instance = SingleInstance(BASE_DIR)
    if not instance.acquire():
        import tkinter as tk
        from tkinter import messagebox
        window = tk.Tk()
        window.withdraw()
        messagebox.showinfo('Program zaten açık',
                            'Bu klasörü kullanan program zaten açık. Açık pencereye dönün.', parent=window)
        window.destroy()
        return
    try:
        from instagram_bot.gui import InstaGUI
        app = InstaGUI()
        app.worker_thread = threading.Thread(target=app.worker_loop, daemon=True)
        app.worker_thread.start()
        app.mainloop()
    finally:
        instance.release()

if __name__ == "__main__":
    main()
