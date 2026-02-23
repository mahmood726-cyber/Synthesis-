"""Comprehensive browser + link audit for all HTML courses in this folder.

Usage:
  python audit_all_courses.py
  python audit_all_courses.py --root "C:\\path\\to\\course\\folder" --sleep 0.1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import JavascriptException, TimeoutException, WebDriverException


IGNORE_CONSOLE_TERMS = ("favicon", "font", "plotly")


@dataclass
class PageResult:
    file: str
    load_errors: List[str] = field(default_factory=list)
    runtime_errors: List[str] = field(default_factory=list)
    nav_errors: List[str] = field(default_factory=list)
    broken_refs: List[str] = field(default_factory=list)
    module_count: int = 0
    slide_checks: int = 0

    def has_issues(self) -> bool:
        return any(
            [
                self.load_errors,
                self.runtime_errors,
                self.nav_errors,
                self.broken_refs,
            ]
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit all local HTML course files.")
    parser.add_argument(
        "--root",
        default=os.path.dirname(os.path.abspath(__file__)),
        help="Folder containing course HTML files (default: script folder).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.06,
        help="Delay after navigation actions in seconds.",
    )
    parser.add_argument(
        "--load-wait",
        type=float,
        default=0.8,
        help="Delay after initial page load in seconds.",
    )
    parser.add_argument(
        "--output",
        default="audit_report.json",
        help="JSON report filename written under --root.",
    )
    return parser.parse_args()


def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(25)
    driver.set_script_timeout(20)
    return driver


def normalized_console_errors(
    driver: webdriver.Chrome,
    seen: Set[Tuple[int, str]],
    ignore_terms: Tuple[str, ...] = IGNORE_CONSOLE_TERMS,
) -> List[str]:
    out: List[str] = []
    for entry in driver.get_log("browser"):
        if entry.get("level") != "SEVERE":
            continue
        msg = entry.get("message", "")
        if any(term in msg.lower() for term in ignore_terms):
            continue
        key = (int(entry.get("timestamp", 0)), msg)
        if key in seen:
            continue
        seen.add(key)
        out.append(msg)
    return out


def get_course_state(driver: webdriver.Chrome) -> Dict[str, object]:
    script = """
    const state = {
      hasModules: false,
      modulesVar: null,
      moduleCount: 0,
      hasGoToModule: typeof goToModule === 'function',
      hasRenderSlides: typeof renderSlides === 'function',
      hasUpdateSlideDisplay: typeof updateSlideDisplay === 'function',
      hasGoToSlide: typeof goToSlide === 'function',
      hasNextSlide: typeof nextSlide === 'function',
      hasPrevSlide: typeof prevSlide === 'function'
    };
    if (typeof modules !== 'undefined' && Array.isArray(modules)) {
      state.hasModules = true;
      state.modulesVar = 'modules';
      state.moduleCount = modules.length;
    } else if (typeof MODULES !== 'undefined' && Array.isArray(MODULES)) {
      state.hasModules = true;
      state.modulesVar = 'MODULES';
      state.moduleCount = MODULES.length;
    }
    return state;
    """
    return driver.execute_script(script)


def navigate_module(driver: webdriver.Chrome, module_index: int, modules_var: str) -> Dict[str, object]:
    script = f"""
    const arr = (typeof modules !== 'undefined' && Array.isArray(modules))
      ? modules
      : ((typeof MODULES !== 'undefined' && Array.isArray(MODULES)) ? MODULES : []);
    const item = arr[{module_index}];
    const candidateId = item && typeof item.id !== 'undefined' ? item.id : {module_index};
    try {{
      if (typeof goToModule === 'function') {{
        goToModule(candidateId);
      }} else {{
        window.currentModule = {module_index};
      }}
      return {{
        ok: true,
        moduleIndex: {module_index},
        candidateId: candidateId,
        currentModule: typeof currentModule !== 'undefined' ? currentModule : null,
        slideCount:
          item && Array.isArray(item.slides) ? item.slides.length :
          item && typeof item.slides === 'number' ? item.slides :
          item && Array.isArray(item.sections) ? item.sections.length :
          item && Array.isArray(item.lessons) ? item.lessons.length :
          0
      }};
    }} catch (e) {{
      return {{ok: false, moduleIndex: {module_index}, candidateId: candidateId, error: String(e)}};
    }}
    """
    return driver.execute_script(script)


def navigate_slide(driver: webdriver.Chrome, slide_index: int) -> Dict[str, object]:
    script = f"""
    try {{
      if (typeof currentSlide !== 'undefined') {{
        currentSlide = {slide_index};
      }}
      // Prefer the course's native slide API when available.
      if (typeof goToSlide === 'function') {{
        goToSlide({slide_index});
      }} else if (typeof renderSlides === 'function') {{
        renderSlides();
      }} else if (typeof updateSlideDisplay === 'function') {{
        updateSlideDisplay();
      }}
      return {{
        ok: true,
        currentSlide: typeof currentSlide !== 'undefined' ? currentSlide : null
      }};
    }} catch (e) {{
      return {{ok: false, error: String(e)}};
    }}
    """
    return driver.execute_script(script)


def exercise_next_prev(driver: webdriver.Chrome) -> List[str]:
    errors: List[str] = []
    for fn_name in ("nextSlide", "prevSlide"):
        try:
            result = driver.execute_script(
                f"""
                try {{
                  if (typeof {fn_name} === 'function') {{
                    {fn_name}();
                    return {{ok: true}};
                  }}
                  return {{ok: true, skipped: true}};
                }} catch (e) {{
                  return {{ok: false, error: String(e)}};
                }}
                """
            )
            if not result.get("ok", False):
                errors.append(f"{fn_name}() failed: {result.get('error', 'unknown error')}")
        except JavascriptException as exc:
            errors.append(f"{fn_name}() invocation threw JS exception: {exc}")
    return errors


def find_broken_local_refs(file_path: str, root: str) -> List[str]:
    with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    attr_re = re.compile(r"\b(?:href|src)\s*=\s*([\"'])(.*?)\1", re.IGNORECASE)
    issues: List[str] = []

    for m in attr_re.finditer(content):
        raw = m.group(2).strip()
        if not raw or "${" in raw:
            continue
        low = raw.lower()
        if low.startswith(("http://", "https://", "data:", "mailto:", "tel:", "javascript:", "#")):
            continue

        clean = raw.split("#", 1)[0].split("?", 1)[0]
        clean = urllib.parse.unquote(clean)
        if not clean or clean.startswith("//"):
            continue

        if clean.startswith("/"):
            target = os.path.join(root, clean.lstrip("/").replace("/", os.sep))
        else:
            target = os.path.join(os.path.dirname(file_path), clean.replace("/", os.sep))

        if not os.path.exists(target):
            line = content.count("\n", 0, m.start()) + 1
            issues.append(f"line {line}: {raw} -> {os.path.relpath(target, root)}")

    return issues


def audit_page(driver: webdriver.Chrome, file_path: str, root: str, sleep_s: float, load_wait: float) -> PageResult:
    result = PageResult(file=os.path.basename(file_path))
    seen_console: Set[Tuple[int, str]] = set()

    url = "file:///" + file_path.replace("\\", "/").replace(" ", "%20")
    try:
        driver.get(url)
    except TimeoutException:
        result.load_errors.append("Page load timeout (>25s)")
        try:
            driver.execute_script("window.stop();")
        except WebDriverException:
            pass
    time.sleep(load_wait)

    result.load_errors.extend(normalized_console_errors(driver, seen_console))
    result.broken_refs.extend(find_broken_local_refs(file_path, root))

    try:
        state = get_course_state(driver)
    except (JavascriptException, WebDriverException) as exc:
        result.runtime_errors.append(f"Could not read page state: {exc}")
        return result

    if not state.get("hasModules"):
        return result

    modules_var = str(state.get("modulesVar"))
    module_count = int(state.get("moduleCount", 0))
    result.module_count = module_count

    for module_index in range(module_count):
        module_nav = navigate_module(driver, module_index, modules_var)
        time.sleep(sleep_s)
        result.runtime_errors.extend(normalized_console_errors(driver, seen_console))

        if not module_nav.get("ok", False):
            result.nav_errors.append(
                f"module {module_index} navigation failed: {module_nav.get('error', 'unknown error')}"
            )
            continue

        slide_count = int(module_nav.get("slideCount", 0))
        if slide_count > 300:
            result.nav_errors.append(
                f"module {module_index} reported unusually high slide count ({slide_count}); capped at 300"
            )
            slide_count = 300
        for slide_index in range(slide_count):
            slide_nav = navigate_slide(driver, slide_index)
            time.sleep(sleep_s)
            result.slide_checks += 1
            result.runtime_errors.extend(normalized_console_errors(driver, seen_console))

            if not slide_nav.get("ok", False):
                result.nav_errors.append(
                    f"module {module_index} slide {slide_index} navigation failed: "
                    f"{slide_nav.get('error', 'unknown error')}"
                )

    result.nav_errors.extend(exercise_next_prev(driver))
    result.runtime_errors.extend(normalized_console_errors(driver, seen_console))
    return result


def main() -> int:
    args = parse_args()
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"Root does not exist or is not a directory: {root}")
        return 2

    html_files = sorted(
        os.path.join(root, f)
        for f in os.listdir(root)
        if f.lower().endswith(".html")
    )
    if not html_files:
        print(f"No HTML files found under: {root}")
        return 2

    driver = build_driver()
    results: List[PageResult] = []

    try:
        for file_path in html_files:
            try:
                page = audit_page(
                    driver=driver,
                    file_path=file_path,
                    root=root,
                    sleep_s=args.sleep,
                    load_wait=args.load_wait,
                )
            except Exception as exc:
                page = PageResult(file=os.path.basename(file_path))
                page.runtime_errors.append(f"Audit exception: {exc}")
                page.runtime_errors.append(traceback.format_exc(limit=1).strip())
            results.append(page)
            print(
                f"[AUDIT] {page.file} | modules={page.module_count} | "
                f"slides_checked={page.slide_checks} | "
                f"issues={len(page.load_errors) + len(page.runtime_errors) + len(page.nav_errors) + len(page.broken_refs)}"
            )
    finally:
        driver.quit()

    summary = {
        "root": root,
        "total_files": len(results),
        "files_with_issues": 0,
        "totals": {
            "load_errors": 0,
            "runtime_errors": 0,
            "nav_errors": 0,
            "broken_refs": 0,
            "slides_checked": 0,
        },
        "files": [],
    }

    for page in results:
        issues_count = (
            len(page.load_errors)
            + len(page.runtime_errors)
            + len(page.nav_errors)
            + len(page.broken_refs)
        )
        if issues_count > 0:
            summary["files_with_issues"] += 1
        summary["totals"]["load_errors"] += len(page.load_errors)
        summary["totals"]["runtime_errors"] += len(page.runtime_errors)
        summary["totals"]["nav_errors"] += len(page.nav_errors)
        summary["totals"]["broken_refs"] += len(page.broken_refs)
        summary["totals"]["slides_checked"] += page.slide_checks
        summary["files"].append(
            {
                "file": page.file,
                "module_count": page.module_count,
                "slides_checked": page.slide_checks,
                "load_errors": page.load_errors,
                "runtime_errors": page.runtime_errors,
                "nav_errors": page.nav_errors,
                "broken_refs": page.broken_refs,
            }
        )

    report_path = os.path.join(root, args.output)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    print("\n=== SUMMARY ===")
    print(json.dumps({k: summary[k] for k in ("total_files", "files_with_issues", "totals")}, indent=2))
    print(f"Report written: {report_path}")

    return 1 if summary["files_with_issues"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
