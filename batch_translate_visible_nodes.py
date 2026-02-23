"""Batch-translate visible English text nodes in selected HTML files.

Designed for large files where single-request-per-string is too slow.
Translates only visible text nodes (not script/style/code/pre/noscript).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Sequence, Tuple

from bs4 import BeautifulSoup, NavigableString


def configure_stdout_utf8() -> None:
    """Best-effort UTF-8 stdout setup without import-time side effects."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return
    except Exception:  # noqa: BLE001
        pass

    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        return
    try:
        sys.stdout = io.TextIOWrapper(buffer, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


WORDS_RE = re.compile(r"\b[A-Za-z][A-Za-z'\-]{2,}\b")
STOP_WORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "to",
    "of",
    "in",
    "on",
    "at",
    "by",
    "and",
    "or",
    "not",
    "for",
    "as",
    "with",
    "from",
    "this",
    "that",
    "these",
    "those",
    "you",
    "your",
    "we",
    "our",
    "course",
    "module",
    "review",
    "analysis",
    "evidence",
    "method",
    "methods",
    "question",
    "answer",
    "results",
    "study",
    "studies",
    "data",
    "tool",
    "dashboard",
    "library",
    "certificate",
    "complete",
    "completed",
}
LANG_ALIASES = {
    "zh": "zh-cn",
    "zh_cn": "zh-cn",
    "zh-hans": "zh-cn",
    "zh-cn": "zh-cn",
}
LANG_CODE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")


def normalize(s: str) -> str:
    text = (s or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", text).strip()


def normalize_lang_code(code: str) -> str:
    key = (code or "").strip().lower().replace("_", "-")
    return LANG_ALIASES.get(key, key)


def to_google_lang(code: str) -> str:
    return "zh-CN" if code == "zh-cn" else code


def is_english_like(text: str, min_words: int = 3) -> bool:
    t = normalize(text)
    if len(t) < 10:
        return False
    words = [w.lower() for w in WORDS_RE.findall(t)]
    if len(words) < min_words:
        return False
    letters = sum(ch.isalpha() for ch in t)
    ascii_letters = sum(("A" <= ch <= "Z") or ("a" <= ch <= "z") for ch in t)
    if letters == 0:
        return False
    if ascii_letters / letters < 0.90:
        return False
    hits = sum(1 for w in words if w in STOP_WORDS)
    return hits >= 2


def translate_google(text: str, lang: str) -> str:
    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": lang, "dt": "t", "q": text}
    )
    url = f"https://translate.googleapis.com/translate_a/single?{params}"
    with urllib.request.urlopen(url, timeout=25) as resp:
        payload = resp.read().decode("utf-8")
    obj = json.loads(payload)
    return "".join(part[0] for part in obj[0]).strip()


def batch_translate(texts: Sequence[str], lang: str) -> Dict[str, str]:
    uniq = []
    seen = set()
    for t in texts:
        if t not in seen:
            uniq.append(t)
            seen.add(t)

    out: Dict[str, str] = {}
    sep = "<<<SEP_TRANSLATE_9F31>>>"
    i = 0
    while i < len(uniq):
        chunk: List[str] = []
        total = 0
        while i < len(uniq):
            nxt = uniq[i]
            add_len = len(nxt) + (len(sep) if chunk else 0)
            if chunk and (len(chunk) >= 35 or total + add_len > 3800):
                break
            chunk.append(nxt)
            total += add_len
            i += 1

        joined = sep.join(chunk)
        try:
            translated = translate_google(joined, lang)
            parts = translated.split(sep)
            if len(parts) == len(chunk):
                for src, tr in zip(chunk, parts):
                    out[src] = tr.strip()
            else:
                raise RuntimeError("separator mismatch")
        except Exception:
            for src in chunk:
                try:
                    out[src] = translate_google(src, lang)
                    time.sleep(0.08)
                except Exception:
                    out[src] = src

        time.sleep(0.06)

    return out


def backup_file(path: str, root: str, backup_base: str | None) -> str | None:
    if not backup_base:
        return None
    rel = os.path.relpath(path, root)
    backup_path = os.path.join(backup_base, rel + ".bak")
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    shutil.copy2(path, backup_path)
    return backup_path


def make_backup_run_dir(root: str, backup_dir: str, run_label: str) -> str:
    base_root = backup_dir if os.path.isabs(backup_dir) else os.path.join(root, backup_dir)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_label).strip("-") or "run"
    nonce = f"{time.time_ns() % 1_000_000_000:09d}"
    run_dir = os.path.join(base_root, f"{stamp}_{safe_label}_{os.getpid()}_{nonce}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def process_file(path: str, root: str, backup_base: str | None, lang: str, dry_run: bool) -> int:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        soup = BeautifulSoup(fh.read(), "html.parser")

    targets: List[Tuple[NavigableString, str, str, str]] = []
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent.name if node.parent else ""
        if parent in {"script", "style", "code", "pre", "noscript"}:
            continue
        raw = str(node)
        stripped = normalize(raw)
        if not is_english_like(stripped, min_words=3):
            continue
        if "http://" in stripped or "https://" in stripped:
            continue
        leading = raw[: len(raw) - len(raw.lstrip())]
        trailing = raw[len(raw.rstrip()) :]
        targets.append((node, stripped, leading, trailing))

    phrases = [t[1] for t in targets]
    mapping = batch_translate(phrases, lang) if phrases else {}

    changes = 0
    for node, src, leading, trailing in targets:
        tr = mapping.get(src, src)
        new = f"{leading}{tr}{trailing}"
        if new != str(node):
            node.replace_with(NavigableString(new))
            changes += 1

    if changes and not dry_run:
        backup_path = backup_file(path, root, backup_base)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(str(soup))
        if backup_path:
            print(f"[BAK] {os.path.relpath(backup_path, root)}")
    return changes


def main() -> int:
    configure_stdout_utf8()
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    p.add_argument("--lang", required=True, help="target language code (e.g., fr, es)")
    p.add_argument("--files", required=True, help="comma-separated filenames")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--backup-dir",
        default="_translation_backups",
        help="Backup root folder (relative to --root, or absolute path). Empty string disables backups. Uses a unique per-run subfolder.",
    )
    args = p.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"[ERR] Root directory not found: {root}")
        return 1
    lang_code = normalize_lang_code(args.lang)
    if not LANG_CODE_RE.fullmatch(lang_code):
        print(f"[ERR] Invalid language code: {args.lang}")
        print("Use codes like: fr, es, de, zh-cn")
        return 1
    lang = to_google_lang(lang_code)
    backup_base: str | None = None
    if not args.dry_run and (args.backup_dir or "").strip():
        backup_base = make_backup_run_dir(
            root,
            args.backup_dir,
            "batch-translate-visible-nodes",
        )
        try:
            shown = os.path.relpath(backup_base, root)
        except ValueError:
            shown = backup_base
        print(f"[BAK] backup run folder: {shown}")
    files = [f.strip() for f in args.files.split(",") if f.strip()]
    if not files:
        print("[ERR] No input files were provided after parsing --files.")
        return 1
    missing = [f for f in files if not os.path.exists(os.path.join(root, f))]
    if missing:
        for name in missing:
            print(f"[ERR] File not found: {name}")
        print(f"\nFile selection errors: {len(missing)}")
        return 1
    total = 0
    for fn in files:
        path = os.path.join(root, fn)
        print(f"[RUN] {fn}")
        c = process_file(path, root, backup_base, lang, args.dry_run)
        total += c
        print(f"[OK] {fn}: {c} replacements")
    print(f"\nTotal replacements: {total}")
    if args.dry_run:
        print("Dry run only. No files written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
