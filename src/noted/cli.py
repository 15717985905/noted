#!/usr/bin/env python3
# ~/Projects/noted/src/noted/cli.py
# 落笔 · Noted 命令行工具

import sys
import os
import json
import urllib.request
import urllib.parse
import urllib.error
import argparse
import subprocess

NOTES_DIR_DEFAULT = os.path.expanduser("~/ai-notes")
PORT_DEFAULT = 8765


def get_notes_dir() -> str:
    return os.environ.get("NOTED_HOME", NOTES_DIR_DEFAULT)


def get_port() -> int:
    port_str = os.environ.get("NOTED_PORT", "")
    if port_str.isdigit():
        return int(port_str)
    return PORT_DEFAULT


def get_base_url() -> str:
    return f"http://localhost:{get_port()}"


def api_get(path):
    try:
        req = urllib.request.Request(f"{get_base_url()}{path}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"无法连接到 hub 服务: {e}")
        print("请先运行: noted serve")
        sys.exit(1)


def cmd_doctor():
    notes_dir = get_notes_dir()
    issues = []
    warnings = []

    print(f"检查笔记目录: {notes_dir}")
    if not os.path.isdir(notes_dir):
        issues.append(f"笔记目录不存在: {notes_dir}")
        print("  状态: 目录不存在")
    elif not os.access(notes_dir, os.W_OK):
        issues.append(f"笔记目录不可写: {notes_dir}")
        print("  状态: 不可写")
    else:
        print("  状态: 正常")

    sync_paths_file = os.path.join(notes_dir, ".sync-paths")
    if os.path.isfile(sync_paths_file):
        print("\n检查 .sync-paths:")
        with open(sync_paths_file, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                expanded = os.path.expanduser(line)
                if os.path.isdir(expanded):
                    print(f"  [{i}] {expanded} - 有效")
                else:
                    issues.append(f"同步路径不存在: {expanded}")
                    print(f"  [{i}] {expanded} - 不存在")
    else:
        print("\n.sync-paths: 未找到（可忽略）")

    print("\n检查破损软链接:")
    broken_links = []
    if os.path.isdir(notes_dir):
        for f in os.listdir(notes_dir):
            if not f.endswith(".md"):
                continue
            path = os.path.join(notes_dir, f)
            if os.path.islink(path) and not os.path.exists(path):
                broken_links.append(f)
    if broken_links:
        for name in broken_links:
            issues.append(f"破损软链接: {name}")
            print(f"  {name} (可执行: rm '{os.path.join(notes_dir, name)}')")
    else:
        print("  无破损软链接")

    print("\n检查 .index.json:")
    index_file = os.path.join(notes_dir, ".index.json")
    if os.path.isfile(index_file):
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                json.load(f)
            print("  可解析")
        except Exception as e:
            issues.append(f".index.json 解析失败: {e}")
            print(f"  解析失败: {e}")
    else:
        print("  未找到（首次运行可忽略）")

    print("\n检查 hub 服务:")
    try:
        req = urllib.request.Request(f"{get_base_url()}/api/list")
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()
        print(f"  运行中 ({get_base_url()})")
    except Exception as e:
        warnings.append(f"hub 服务未运行: {e}")
        print(f"  未运行 ({get_base_url()}): {e}")

    print("\n" + "=" * 40)
    if issues:
        print(f"发现 {len(issues)} 个问题，{len(warnings)} 个警告")
        for issue in issues:
            print(f"  [问题] {issue}")
        for warning in warnings:
            print(f"  [警告] {warning}")
        sys.exit(1)
    elif warnings:
        print(f"健康（{len(warnings)} 个警告）")
        for warning in warnings:
            print(f"  [警告] {warning}")
    else:
        print("健康，无问题")


def cmd_open():
    os.system(f"open {get_base_url()}")


def cmd_list():
    notes = api_get("/api/list")
    if not notes:
        print("暂无总结")
        return
    for n in notes:
        tags = ", ".join(n.get("tags", [])) or "无标签"
        symlink = " [软链接]" if n.get("is_symlink") else ""
        print(f"[{n['date']}] {n['title']}{symlink}")
        print(f"  标签: {tags}")
        if n.get("source_label"):
            print(f"  来源: {n['source_label']}")
        print(f"  摘要: {n['excerpt']}")
        print()


def cmd_search(term):
    if not term:
        print("用法: noted search <关键词>")
        sys.exit(1)
    notes = api_get(f"/api/search?q={urllib.parse.quote(term)}")
    if not notes:
        print(f"未找到包含 '{term}' 的总结")
        return
    print(f"找到 {len(notes)} 条匹配:\n")
    for n in notes:
        tags = ", ".join(n.get("tags", [])) or "无标签"
        symlink = " [软链接]" if n.get("is_symlink") else ""
        print(f"[{n['date']}] {n['title']}{symlink}")
        print(f"  标签: {tags}")
        if n.get("source_label"):
            print(f"  来源: {n['source_label']}")
        print(f"  摘要: {n['excerpt']}")
        print()


def cmd_sync():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from noted.hub import sync_links, get_notes_dir
        notes_dir = get_notes_dir()
        new_links = sync_links(notes_dir)
        print(f"同步完成，新增 {len(new_links)} 个软链接")
        for link in new_links:
            print(f"  + {link}")
        if not new_links:
            print("  没有新文件需要同步")
    except ImportError as e:
        print(f"无法导入 sync 模块: {e}")
        sys.exit(1)


def cmd_add_path(path):
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        print(f"目录不存在: {path}")
        sys.exit(1)
    sync_paths_file = os.path.join(get_notes_dir(), ".sync-paths")
    with open(sync_paths_file, "a") as f:
        f.write(f"\n{path}\n")
    print(f"已添加同步路径: {path}")


def cmd_discover():
    try:
        req = urllib.request.Request(f"{get_base_url()}/api/discover")
        with urllib.request.urlopen(req, timeout=10) as resp:
            candidates = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"无法连接到 hub 服务: {e}")
        print("请先运行: noted serve")
        sys.exit(1)
    if not candidates:
        print("未发现候选文件")
        return
    print(f"发现 {len(candidates)} 个候选文件:\n")
    for i, c in enumerate(candidates, 1):
        print(f"[{i}] {c['filename']}")
        print(f"    大小: {(c['size'] / 1024):.1f} KB · 修改: {c['modified']}")
        print(f"    来源: {c.get('source_label', '')}")
        print(f"    判定: {', '.join(c['reasons'])}")
        print()
    print(f"请打开浏览器 {get_base_url()} 在侧栏点击「发现未入库总结」进行勾选入库")


def cmd_serve(args):
    from noted.hub import run_server
    run_server(port=args.port, notes_dir=args.notes_dir)


def main():
    parser = argparse.ArgumentParser(description="落笔 · Noted 命令行工具")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("open", help="打开浏览器阅读页")
    subparsers.add_parser("list", help="列出所有总结")
    search_parser = subparsers.add_parser("search", help="搜索总结")
    search_parser.add_argument("term", help="搜索关键词")
    subparsers.add_parser("sync", help="手动同步软链接")
    subparsers.add_parser("discover", help="发现未入库总结")
    subparsers.add_parser("doctor", help="运行健康检查")
    add_path_parser = subparsers.add_parser("add-path", help="添加同步目录")
    add_path_parser.add_argument("path", help="目录路径")

    serve_parser = subparsers.add_parser("serve", help="启动本地网页服务")
    serve_parser.add_argument("--port", type=int, default=None, help=f"服务端口，默认 {PORT_DEFAULT}")
    serve_parser.add_argument("--notes-dir", default=None, help=f"笔记目录，默认 {NOTES_DIR_DEFAULT}")

    args = parser.parse_args()
    if not args.command:
        cmd_open()
        return

    if args.command == "serve":
        cmd_serve(args)
        return

    if args.command == "search":
        cmd_search(args.term)
    elif args.command == "list":
        cmd_list()
    elif args.command == "sync":
        cmd_sync()
    elif args.command == "discover":
        cmd_discover()
    elif args.command == "doctor":
        cmd_doctor()
    elif args.command == "add-path":
        cmd_add_path(args.path)
    else:
        cmd_open()


if __name__ == "__main__":
    main()