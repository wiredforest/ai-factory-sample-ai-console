"""自分専用の AI 会話コンソールを起動する。使い方: python app.py [--port PORT]"""

import sys

from console.server import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
