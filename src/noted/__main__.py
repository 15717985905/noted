import argparse
from noted.hub import run_server, get_port, DEFAULT_PORT

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="落笔 · Noted 本地网页服务")
    parser.add_argument("--notes-dir", help="笔记目录路径，默认 ~/ai-notes", default=None)
    parser.add_argument("--port", type=int, help=f"服务端口，默认 {DEFAULT_PORT}", default=None)
    args = parser.parse_args()
    run_server(port=args.port, notes_dir=args.notes_dir)