"""Opens the perfume comparison tool in its own window (used to build the .exe / .app)."""
import socket
import threading
import time
import urllib.request
import webbrowser

import perfume_compare as pc


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    port = free_port()
    threading.Thread(target=lambda: pc.app.run(host="127.0.0.1", port=port, use_reloader=False),
                     daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):  # wait until the local server is up
        try:
            urllib.request.urlopen(url, timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    try:
        import webview
        webview.create_window("Perfume Price Check", url, width=980, height=780)
        webview.start()
    except Exception:  # no window support: fall back to the default browser
        webbrowser.open(url)
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
