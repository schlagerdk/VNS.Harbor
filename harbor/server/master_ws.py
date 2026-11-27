"""A terminal or task of the admin on a member, for the master (harbor master ws): run with the admin's Python, which has
simple_websocket. The master's lines of JSON come on stdin and go to the admin's websocket here; what it sends comes out
on stdout, a line each. The token is in HARBOR_MASTER_TOKEN, never on the command line."""
import os
import sys
import threading

from simple_websocket import Client, ConnectionClosed


def main(port, path):
    # Its Host is the address without the port (simple_websocket's), and the admin's terminal wants the Origin the same
    headers = {"X-Harbor-Master": os.environ.get("HARBOR_MASTER_TOKEN", ""), "Origin": "http://127.0.0.1"}
    # For one of the master's users: as that user
    if os.environ.get("HARBOR_MASTER_AS"):
        headers["X-Harbor-As"] = os.environ["HARBOR_MASTER_AS"]
    ws = Client.connect(f"ws://127.0.0.1:{port}{path}", headers=headers)

    def down():
        try:
            while True:
                message = ws.receive()
                if message is None:
                    break
                sys.stdout.write((message if isinstance(message, str) else message.decode("utf-8", "replace")) + "\n")
                sys.stdout.flush()
        except (ConnectionClosed, OSError):
            pass
        os._exit(0)

    threading.Thread(target=down, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            ws.send(line)
        except (ConnectionClosed, OSError):
            break
    ws.close()
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
