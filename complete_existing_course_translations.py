"""Complete translations for existing localized course pages.

Targets only files that already have language suffixes (e.g., -es, -fr),
and translates remaining English-like user-facing text in:
1) HTML text nodes (outside script/style/code/pre)
2) Common UI attributes (title/aria-label/placeholder/alt)
3) Safe JavaScript string literals (strictly filtered)

Usage:
  python complete_existing_course_translations.py --dry-run
  python complete_existing_course_translations.py
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
import traceback
import urllib.parse
import urllib.request
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup, NavigableString


LANG_SUFFIX_RE = re.compile(r"-(ar|de|es|fr|hi|it|ja|ko|pt|ru|zh)\.html$", re.IGNORECASE)
WORDS_RE = re.compile(r"\b[A-Za-z][A-Za-z'\-]{2,}\b")
SUSPICIOUS_JS_KEY_RE = re.compile(
    r"(?:^|[,{]\s*)(?:texte|cons\u00e9quence|r\u00e9sultat|v\u00e9rit\u00e9|histoire)\s*:",
    re.IGNORECASE,
)
SUSPICIOUS_TRUTH_VALUE_RE = re.compile(
    r"(?:^|[,{]\s*)truth\s*:\s*(?!true\b|false\b)",
    re.IGNORECASE,
)
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
    "into",
    "about",
    "over",
    "under",
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
    "story",
    "question",
    "answer",
    "results",
    "study",
    "studies",
    "data",
    "tool",
    "tools",
    "dashboard",
    "library",
    "certificate",
    "complete",
    "completed",
    "download",
}
CORE_ENGLISH_WORDS = {
    "the",
    "is",
    "are",
    "were",
    "to",
    "of",
    "and",
    "or",
    "not",
    "for",
    "with",
    "from",
    "into",
    "about",
    "over",
    "under",
    "this",
    "that",
    "these",
    "those",
    "you",
    "your",
    "we",
    "our",
}


LANG_ALIASES = {
    "zh": "zh-cn",
    "zh_cn": "zh-cn",
    "zh-hans": "zh-cn",
    "zh-cn": "zh-cn",
}


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Complete translations in existing localized pages.")
    parser.add_argument(
        "--root",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="Project folder containing HTML files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing files.",
    )
    parser.add_argument(
        "--langs",
        default="",
        help="Optional comma-separated language subset (e.g., es,fr).",
    )
    parser.add_argument(
        "--files",
        default="",
        help="Optional comma-separated file subset (e.g., index-es.html,grade-certainty-course-es.html).",
    )
    parser.add_argument(
        "--include-scripts",
        action="store_true",
        help="Also translate safe JavaScript string literals (slower).",
    )
    parser.add_argument(
        "--backup-dir",
        default="_translation_backups",
        help="Backup root folder (relative to --root, or absolute path). Empty string disables backups. Uses a unique per-run subfolder.",
    )
    parser.add_argument(
        "--skip-script-integrity-guard",
        action="store_true",
        help="Skip post-translation script integrity guard checks before write (use only for targeted recovery).",
    )
    return parser.parse_args()


def normalize(s: str) -> str:
    text = (s or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", text).strip()


def normalize_lang_code(code: str) -> str:
    key = (code or "").strip().lower().replace("_", "-")
    return LANG_ALIASES.get(key, key)


def to_google_lang(code: str) -> str:
    return "zh-CN" if code == "zh-cn" else code


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


def english_score(text: str) -> float:
    words = [w.lower() for w in WORDS_RE.findall(text)]
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in STOP_WORDS)
    return hits / max(len(words), 1)


def is_english_like(text: str, min_words: int = 3) -> bool:
    text = normalize(text)
    if len(text) < 8:
        return False
    words = [w.lower() for w in WORDS_RE.findall(text)]
    if len(words) < min_words:
        return False
    letters = sum(ch.isalpha() for ch in text)
    ascii_letters = sum(("A" <= ch <= "Z") or ("a" <= ch <= "z") for ch in text)
    if letters == 0:
        return False
    ratio = ascii_letters / letters
    if ratio < 0.85:
        return False
    core_hits = sum(1 for w in words if w in CORE_ENGLISH_WORDS)
    hit_count = sum(1 for w in words if w in STOP_WORDS)
    if len(words) <= 4:
        return core_hits >= 1
    return hit_count >= 2 or english_score(text) >= 0.22


def translate_google(text: str, lang: str, cache: Dict[Tuple[str, str], str]) -> str:
    text = normalize(text)
    if not text:
        return text
    key = (lang, text)
    if key in cache:
        return cache[key]

    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": lang, "dt": "t", "q": text}
    )
    url = f"https://translate.googleapis.com/translate_a/single?{params}"

    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                payload = resp.read().decode("utf-8")
            obj = json.loads(payload)
            translated = "".join(part[0] for part in obj[0]).strip()
            if translated:
                cache[key] = translated
                return translated
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"Translate failed for lang={lang}, text={text[:80]!r}: {last_err}")


def batch_translate_texts(
    texts: List[str],
    lang: str,
    cache: Dict[Tuple[str, str], str],
) -> None:
    uniq: List[str] = []
    seen: set[str] = set()
    for text in texts:
        normalized = normalize(text)
        if not normalized:
            continue
        key = (lang, normalized)
        if key in cache:
            continue
        if normalized in seen:
            continue
        uniq.append(normalized)
        seen.add(normalized)

    if not uniq:
        return

    sep = "<<<SYNC_SEP_A12F>>>"
    i = 0
    while i < len(uniq):
        chunk: List[str] = []
        total = 0
        while i < len(uniq):
            nxt = uniq[i]
            add_len = len(nxt) + (len(sep) if chunk else 0)
            if chunk and (len(chunk) >= 40 or total + add_len > 3900):
                break
            chunk.append(nxt)
            total += add_len
            i += 1

        joined = sep.join(chunk)
        try:
            translated = translate_google(joined, lang, cache)
            parts = translated.split(sep)
            if len(parts) != len(chunk):
                raise RuntimeError("separator mismatch")
            for src, tr in zip(chunk, parts):
                out = tr.strip()
                cache[(lang, src)] = out if out else src
        except Exception:  # noqa: BLE001
            for src in chunk:
                try:
                    translate_google(src, lang, cache)
                except Exception:  # noqa: BLE001
                    cache[(lang, src)] = src
                time.sleep(0.08)
        time.sleep(0.05)


def should_skip_text_node(parent_tag_name: str, text: str) -> bool:
    if parent_tag_name in {"script", "style", "code", "pre", "noscript"}:
        return True
    t = normalize(text)
    if not t:
        return True
    if t.startswith(("/*", "//", "<!--")):
        return True
    if "http://" in t or "https://" in t:
        return True
    if "<-" in t:
        return True
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)", t) and "=" in t and "," in t:
        return True
    # Decorative module separators are intentionally stylized and often already localized.
    if t.count("=") >= 8 and "MODULE" in t.upper():
        return True
    return False


def translate_html_text_nodes(soup: BeautifulSoup, lang: str, cache: Dict[Tuple[str, str], str]) -> int:
    targets: List[Tuple[NavigableString, str, str, str]] = []
    phrases: List[str] = []
    for node in list(soup.find_all(string=True)):
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent.name if node.parent else ""
        original = str(node)
        if should_skip_text_node(parent, original):
            continue
        raw = original
        stripped = normalize(raw)
        if not is_english_like(stripped, min_words=3):
            continue
        leading = raw[: len(raw) - len(raw.lstrip())]
        trailing = raw[len(raw.rstrip()) :]
        targets.append((node, stripped, leading, trailing))
        phrases.append(stripped)

    batch_translate_texts(phrases, lang, cache)

    changes = 0
    for node, stripped, leading, trailing in targets:
        translated = cache.get((lang, stripped), stripped)
        replacement = f"{leading}{translated}{trailing}"
        if replacement != str(node):
            node.replace_with(NavigableString(replacement))
            changes += 1
    return changes


def translate_attributes(soup: BeautifulSoup, lang: str, cache: Dict[Tuple[str, str], str]) -> int:
    targets: List[Tuple[object, str, str]] = []
    phrases: List[str] = []
    changes = 0
    attrs = ("title", "aria-label", "placeholder", "alt")
    for tag in soup.find_all(True):
        for attr in attrs:
            val = tag.get(attr)
            if not isinstance(val, str):
                continue
            stripped = normalize(val)
            if not is_english_like(stripped, min_words=2):
                continue
            targets.append((tag, attr, stripped))
            phrases.append(stripped)

    batch_translate_texts(phrases, lang, cache)

    for tag, attr, stripped in targets:
        translated = cache.get((lang, stripped), stripped)
        if translated != stripped:
            tag[attr] = translated
            changes += 1
    return changes


def is_safe_js_literal_candidate(text: str) -> bool:
    s = normalize(text)
    if not is_english_like(s, min_words=3):
        return False
    words = [w.lower() for w in WORDS_RE.findall(s)]
    core_hits = sum(1 for w in words if w in CORE_ENGLISH_WORDS)
    # Require core English function-word signal to avoid re-translating already localized strings.
    if core_hits == 0:
        return False
    if len(s) > 300:
        return False
    if s.startswith((",", ":", ".")) or s.endswith((",", ":")):
        return False
    # Guard against regex-literal parsing artifacts that look like JS object fragments.
    if "," in s and ":" in s:
        return False
    if re.search(
        r"\b(?:id|text|result|consequence|truth|story|module|title|situation|question|choices|correct|principle|lesson)\s*:",
        s,
    ):
        return False
    unsafe_parts = (
        "http://",
        "https://",
        "localStorage",
        "querySelector",
        "getElementById",
        "classList",
        "addEventListener",
        "window.",
        "document.",
        "function(",
        "=>",
        "return ",
        "if (",
        "for (",
        "while (",
    )
    if any(p in s for p in unsafe_parts):
        return False
    # Avoid selector-ish/code-ish strings
    if re.search(r"[#\[\]{};=<>]", s):
        return False
    return True


def escape_js_literal(text: str, quote: str) -> str:
    text = text.replace("\\", "\\\\")
    if quote == "'":
        text = text.replace("'", "\\'")
    else:
        text = text.replace('"', '\\"')
    return text


def translate_script_text(script_text: str, lang: str, cache: Dict[Tuple[str, str], str]) -> Tuple[str, int]:
    if not script_text:
        return script_text, 0

    # Match JS single/double-quoted literals with basic escape support.
    lit_re = re.compile(r"(?P<q>['\"])(?P<body>(?:\\.|(?!\1).)*?)(?P=q)", re.S)
    literal_items: List[Tuple[int, int, str, str, str | None]] = []
    phrases: List[str] = []

    for m in lit_re.finditer(script_text):
        start, end = m.span()
        quote = m.group("q")
        body = m.group("body")
        raw_body = body
        candidate = raw_body.replace("\\n", " ").replace("\\t", " ")
        candidate = re.sub(r"\\u[0-9a-fA-F]{4}", " ", candidate)
        candidate = re.sub(r"\\x[0-9a-fA-F]{2}", " ", candidate)
        candidate = normalize(candidate)

        safe_candidate: str | None = None
        if is_safe_js_literal_candidate(candidate):
            safe_candidate = candidate
            phrases.append(candidate)
        literal_items.append((start, end, quote, script_text[start:end], safe_candidate))

    batch_translate_texts(phrases, lang, cache)

    changes = 0
    parts: List[str] = []
    last = 0
    for start, end, quote, original_literal, safe_candidate in literal_items:
        parts.append(script_text[last:start])
        if safe_candidate:
            translated = cache.get((lang, safe_candidate), safe_candidate)
            if translated != safe_candidate:
                new_body = escape_js_literal(translated, quote)
                parts.append(f"{quote}{new_body}{quote}")
                changes += 1
            else:
                parts.append(original_literal)
        else:
            parts.append(original_literal)
        last = end

    parts.append(script_text[last:])
    return "".join(parts), changes


def translate_scripts(soup: BeautifulSoup, lang: str, cache: Dict[Tuple[str, str], str]) -> int:
    changes = 0
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        text = script.string if script.string is not None else script.get_text()
        if not text or "function" not in text and "const " not in text and "let " not in text:
            continue
        new_text, c = translate_script_text(text, lang, cache)
        if c > 0 and new_text != text:
            script.string = new_text
            changes += c
    return changes


def detect_script_translation_risks(soup: BeautifulSoup) -> List[str]:
    lit_re = re.compile(r"(?P<q>['\"])(?P<body>(?:\\.|(?!\1).)*?)(?P=q)", re.S)

    def mask_js_strings(script_text: str) -> str:
        chars = list(script_text)
        for m in lit_re.finditer(script_text):
            for i in range(m.start(), m.end()):
                if chars[i] != "\n":
                    chars[i] = " "
        return "".join(chars)

    issues: List[str] = []
    for idx, script in enumerate(soup.find_all("script"), start=1):
        if script.get("src"):
            continue
        text = script.string if script.string is not None else script.get_text()
        if not text:
            continue
        masked = mask_js_strings(text)
        if "\ufffd" in text:
            issues.append(f"script#{idx}: contains replacement character U+FFFD")
        if SUSPICIOUS_JS_KEY_RE.search(masked):
            issues.append(f"script#{idx}: contains suspicious localized object keys")
        if SUSPICIOUS_TRUTH_VALUE_RE.search(masked):
            issues.append(f"script#{idx}: contains non-boolean `truth` value")
    return issues


def main() -> int:
    configure_stdout_utf8()
    args = parse_args()
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"[ERR] Root directory not found: {root}", flush=True)
        return 1
    selected = {
        normalize_lang_code(x.strip())
        for x in args.langs.split(",")
        if x.strip()
    }
    selected_files = {x.strip() for x in args.files.split(",") if x.strip()}

    html_files = sorted(
        f for f in os.listdir(root) if f.lower().endswith(".html") and LANG_SUFFIX_RE.search(f)
    )
    available_langs = {
        normalize_lang_code(m.group(1))
        for f in html_files
        for m in [LANG_SUFFIX_RE.search(f)]
        if m
    }
    cache: Dict[Tuple[str, str], str] = {}
    total_changes = 0
    guard_blocks = 0
    file_errors = 0
    selection_errors = 0
    processed_files = 0
    if selected:
        unknown_langs = sorted(lang for lang in selected if lang not in available_langs)
        for lang in unknown_langs:
            selection_errors += 1
            print(f"[ERR] requested language not available for localized targets: {lang}", flush=True)
    if selected_files:
        localized_targets = set(html_files)
        missing = sorted(f for f in selected_files if f not in localized_targets)
        for name in missing:
            selection_errors += 1
            print(
                f"[ERR] requested file not found among localized targets: {name}",
                flush=True,
            )
    if selection_errors:
        print("[ERR] Aborting before processing due to selection errors.", flush=True)
        return 1
    backup_base: str | None = None
    if not args.dry_run and (args.backup_dir or "").strip():
        backup_base = make_backup_run_dir(
            root,
            args.backup_dir,
            "complete-existing-translations",
        )
        try:
            shown = os.path.relpath(backup_base, root)
        except ValueError:
            shown = backup_base
        print(f"[BAK] backup run folder: {shown}", flush=True)

    for fn in html_files:
        m = LANG_SUFFIX_RE.search(fn)
        if not m:
            continue
        if selected_files and fn not in selected_files:
            continue
        lang_code = normalize_lang_code(m.group(1))
        if selected and lang_code not in selected:
            continue
        lang = to_google_lang(lang_code)
        processed_files += 1

        try:
            path = os.path.join(root, fn)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                html = fh.read()
            soup = BeautifulSoup(html, "html.parser")

            print(f"[RUN] {fn} ({lang}) ...", flush=True)
            c1 = translate_html_text_nodes(soup, lang, cache)
            c2 = translate_attributes(soup, lang, cache)
            c3 = translate_scripts(soup, lang, cache) if args.include_scripts else 0
            file_changes = c1 + c2 + c3

            if file_changes > 0 and not args.dry_run:
                if args.include_scripts:
                    if not args.skip_script_integrity_guard:
                        risks = detect_script_translation_risks(soup)
                        if risks:
                            guard_blocks += 1
                            print(
                                f"[ERR] {fn} ({lang}): script integrity guard blocked write",
                                flush=True,
                            )
                            for issue in risks[:5]:
                                print(f"      - {issue}", flush=True)
                            continue
                backup_path = backup_file(path, root, backup_base)
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(str(soup))
                if backup_path:
                    print(f"[BAK] {os.path.relpath(backup_path, root)}", flush=True)

            total_changes += file_changes

            print(f"[OK] {fn} ({lang}) -> {file_changes} replacements", flush=True)
        except Exception as exc:  # noqa: BLE001
            file_errors += 1
            print(f"[ERR] {fn} ({lang}): {exc}", flush=True)
            print(traceback.format_exc(limit=1), flush=True)

    if (selected or selected_files) and processed_files == 0:
        selection_errors += 1
        print("[ERR] no localized files matched the provided --langs/--files filters", flush=True)

    print(f"\nTotal replacements: {total_changes}")
    print(f"Cached phrases: {len(cache)}")
    if guard_blocks:
        print(f"Guard blocks: {guard_blocks}")
    if selection_errors:
        print(f"Selection errors: {selection_errors}")
    if file_errors:
        print(f"File errors: {file_errors}")
    if args.dry_run:
        print("Dry run only. No files written.")
    return 1 if (guard_blocks or selection_errors or file_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
