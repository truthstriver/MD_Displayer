import re
import json
import uuid
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, abort, send_from_directory

import markdown
import bleach
from bleach.css_sanitizer import CSSSanitizer

app = Flask(__name__)

# ---- Configuration ----
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / 'uploads'
EXTRACT_DIR = BASE_DIR / 'extracted'
METADATA_FILE = BASE_DIR / 'metadata.json'
ACCESS_KEY = 'display'

UPLOAD_DIR.mkdir(exist_ok=True)
EXTRACT_DIR.mkdir(exist_ok=True)


# ---- Metadata helpers ----

def load_metadata() -> dict:
    if METADATA_FILE.exists():
        try:
            with open(METADATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {'items': []}


def save_metadata(meta: dict) -> None:
    with open(METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


# ---- Access control ----

@app.before_request
def check_access():
    """Block requests missing the required ?key_=display parameter."""
    key = request.args.get('key_', '')
    if key != ACCESS_KEY:
        abort(403)


@app.context_processor
def inject_key_param():
    """Make the key param available in all Jinja templates."""
    return {'key_param': f'key_={ACCESS_KEY}'}


# ---- Markdown → HTML renderer (Python-Markdown + pymdown-extensions) ----

# Allowed HTML tags and attributes after markdown rendering (defence-in-depth)
ALLOWED_TAGS = {
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'p', 'br', 'hr',
    'a', 'img',
    'strong', 'em', 'b', 'i', 'u', 'del', 's',
    'code', 'pre',
    'blockquote',
    'ul', 'ol', 'li',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'span', 'div',
    'sup', 'sub',
    'dl', 'dt', 'dd',
    'input',  # for task lists
    'details', 'summary',
}

ALLOWED_ATTRS = {
    '*': ['class', 'id'],
    'a': ['href', 'title', 'target', 'rel'],
    'img': ['src', 'alt', 'title', 'width', 'height'],
    'div': ['style'],
    'span': ['style'],
    'p': ['style'],
    'th': ['style'],
    'td': ['style'],
    'input': ['type', 'checked', 'disabled'],
    'details': ['open'],
    'pre': ['class'],
}


def _make_md() -> markdown.Markdown:
    """Create a configured Python-Markdown instance."""
    return markdown.Markdown(
        extensions=[
            'pymdownx.arithmatex',     # LaTeX math via KaTeX
            'pymdownx.highlight',       # code syntax highlighting
            'pymdownx.superfences',     # nested / advanced fenced code blocks
            'pymdownx.tasklist',        # GFM task lists
            'pymdownx.tilde',           # ~~strikethrough~~ and ~subscript~
            'pymdownx.caret',           # ^superscript^
            'pymdownx.magiclink',       # auto-link URLs and emails
            'markdown.extensions.tables',        # GFM tables
            'markdown.extensions.toc',           # [TOC]
            'markdown.extensions.def_list',      # definition lists
            'markdown.extensions.abbr',          # abbreviations
            'markdown.extensions.md_in_html',    # process Markdown inside HTML blocks
        ],
        extension_configs={
            'pymdownx.arithmatex': {
                'generic': True,     # output \(...\) / \[...\] for KaTeX to render
                'smart_dollar': True,  # process $...$ and $$...$$ properly before other extensions
            },
            'pymdownx.highlight': {
                'linenums': False,
                'guess_lang': True,
            },
            'pymdownx.tasklist': {
                'custom_checkbox': True,
                'clickable_checkbox': False,
            },
            'pymdownx.magiclink': {
                'repo_url_shortener': True,
                'repo_url_shorthand': True,
                'social_url_shorthand': True,
            },
        },
    )


# Regex to match display-math blocks delimited by $$ at the start of a line
# (possibly indented, e.g. inside list items / blockquotes).
# Group 1 captures the full line prefix (blockquote markers + whitespace)
# so we can preserve the indentation context when inserting surrounding blank lines.
_DISPLAY_MATH_RE = re.compile(
    r'^((?:>[ \t]*)*)([ \t]*)\$\$(.+?)\$\$',
    re.MULTILINE | re.DOTALL,
)


def _normalize_display_math(text: str) -> str:
    """
    Ensure $$...$$ display-math blocks are surrounded by blank lines so that
    smart_dollar can recognise them as block-level math (not inline paragraphs).

    Preserves the indentation of the $$ line so that math inside list items /
    blockquotes does not break the surrounding Markdown structure.
    """
    def _fix(m: re.Match) -> str:
        # Full line prefix = blockquote markers (group 1) + whitespace indent (group 2)
        prefix_str = m.group(1) + m.group(2)
        math = m.group(3)     # the LaTeX content between $$ and $$
        start, end = m.start(), m.end()

        # ---- prefix: ensure a blank line before the $$ block ----
        if start == 0:
            prefix = ''
        elif text[start - 2:start] == '\n\n':
            prefix = ''                          # already preceded by blank line
        elif text[start - 1] == '\n':
            prefix = '\n' + prefix_str           # single \n → add one more (indented)
        else:
            prefix = '\n\n' + prefix_str         # no newline → add blank line (indented)

        # ---- suffix: ensure a blank line after the $$ block ----
        if end == len(text):
            suffix = ''
        elif text[end:end + 2] == '\n\n':
            suffix = ''                          # already followed by blank line
        elif text[end] == '\n':
            suffix = '\n' + prefix_str           # single \n → add one more (indented)
        else:
            suffix = '\n\n' + prefix_str         # no newline → add blank line (indented)

        return f'{prefix}$${math}$${suffix}'

    return _DISPLAY_MATH_RE.sub(_fix, text)


# Regex to detect a numbered list item at column 0 (e.g. "1. ", "12. ")
_LIST_ITEM_RE = re.compile(r'^\d+\.\s')


def _ensure_list_item_separation(text: str) -> str:
    """
    After _normalize_display_math inserts blank lines around $$ blocks,
    loosely-structured lists may have their items merged because the
    blank lines turn tight lists into loose ones, causing Python-Markdown
    to treat subsequent numbered markers as paragraph text.

    This function inserts a blank line before any numbered list item
    (at column 0) that follows an indented continuation line, so that
    the list items stay separated.
    """
    lines = text.split('\n')
    result = []

    for i, line in enumerate(lines):
        stripped = line.lstrip(' ')
        is_list_marker = bool(_LIST_ITEM_RE.match(stripped)) and line[0] != ' ' and line[0] != '\t'
        is_indented_prev = (
            i > 0
            and lines[i - 1].strip() != ''
            and (lines[i - 1].startswith(' ') or lines[i - 1].startswith('\t'))
        )
        prev_is_blank = i > 0 and lines[i - 1].strip() == ''

        if is_list_marker and is_indented_prev and not prev_is_blank:
            result.append('')  # insert blank line to separate list items

        result.append(line)

    return '\n'.join(result)


# Regex to match opening tags of HTML block‑level elements
_BLOCK_HTML_RE = re.compile(
    r'<(div|section|article|aside|header|footer|main|nav|figure)\b([^>]*)>',
    re.IGNORECASE,
)


def _inject_markdown_attr(text: str) -> str:
    """
    Add markdown="1" to HTML block‑level elements that don't already have it,
    so that the md_in_html extension processes Markdown inside them.
    """
    def _replace(m: re.Match) -> str:
        tag_name = m.group(1)
        rest = m.group(2)
        if re.search(r'markdown\s*=\s*["\']', rest, re.IGNORECASE):
            # Already has a markdown attribute — leave untouched
            return m.group(0)
        return f'<{tag_name}{rest} markdown="1">'

    return _BLOCK_HTML_RE.sub(_replace, text)


# Regex: "0) ", "1) ", "12) " at the beginning of a line (possibly indented)
_N_PAREN_LIST_RE = re.compile(r'^([ \t]*)(\d+)\)\s', re.MULTILINE)


def _normalize_ordered_list_markers(text: str) -> str:
    """
    Convert N)-style ordered list markers to N. (standard Markdown syntax).
    E.g. "0) item" → "0. item", "  1) item" → "  1. item".
    """
    return _N_PAREN_LIST_RE.sub(r'\1\2. ', text)


def render_markdown(text: str, item_id: str = '') -> str:
    """
    Convert markdown text to HTML using Python-Markdown + pymdown-extensions.

    Args:
        text: Raw markdown content.
        item_id: The item's extraction directory ID (for rewriting image URLs).
    """
    # Pre-process: normalise blank lines around $$ display-math blocks
    text = _normalize_display_math(text)

    # Pre-process: ensure list items are properly separated after
    # _normalize_display_math may have inserted blank lines that turn
    # tight lists into loose ones, causing item markers to merge.
    text = _ensure_list_item_separation(text)

    # Pre-process: inject markdown="1" into HTML block elements so that
    # the md_in_html extension processes Markdown inside them
    text = _inject_markdown_attr(text)

    # Pre-process: convert "1) " → "1. " so that ordered lists are
    # recognised even when the author used parentheses instead of dots
    text = _normalize_ordered_list_markers(text)

    md = _make_md()
    raw_html = md.convert(text)

    # Sanitize the HTML (defence-in-depth against XSS)
    # Allow common inline-style CSS properties for div/span/p styling
    css_sanitizer = CSSSanitizer(
        allowed_css_properties=[
            'background', 'background-color', 'background-image',
            'border', 'border-left', 'border-right', 'border-top', 'border-bottom',
            'border-radius', 'border-color', 'border-width', 'border-style',
            'color', 'padding', 'padding-left', 'padding-right', 'padding-top', 'padding-bottom',
            'margin', 'margin-left', 'margin-right', 'margin-top', 'margin-bottom',
            'width', 'max-width', 'min-width', 'height', 'max-height', 'min-height',
            'display', 'text-align', 'text-decoration', 'text-transform',
            'font-family', 'font-size', 'font-weight', 'font-style',
            'line-height', 'letter-spacing', 'word-spacing',
            'white-space', 'word-break', 'overflow', 'overflow-x', 'overflow-y',
            'box-shadow', 'opacity', 'cursor', 'float', 'clear', 'position',
            'top', 'right', 'bottom', 'left', 'z-index',
            'vertical-align', 'list-style', 'list-style-type',
            'box-sizing', 'transform', 'transition',
        ],
    )
    clean_html = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        css_sanitizer=css_sanitizer,
        strip=True,
    )

    # Rewrite relative image src to point to our file-serving route
    if item_id:
        clean_html = _rewrite_image_urls(clean_html, item_id)

    return clean_html


_IMG_SRC_RE = re.compile(r'src="(?!https?://|/files/|data:)([^"]+)"')


def _rewrite_image_urls(html_content: str, item_id: str) -> str:
    """Rewrite relative image src URLs to use the /files/<item_id>/ route."""
    def _replace(m: re.Match) -> str:
        rel_path = m.group(1)
        return f'src="/files/{item_id}/{rel_path}?key_={ACCESS_KEY}"'

    return _IMG_SRC_RE.sub(_replace, html_content)


# ---- Static file serving for extracted content ----

@app.route('/files/<item_id>/<path:filename>')
def serve_extracted_file(item_id: str, filename: str):
    """Serve a file from within an extracted ZIP directory (e.g. images in res/)."""
    if not re.match(r'^[0-9a-f]{12}$', item_id):
        abort(404)

    extract_path = EXTRACT_DIR / item_id
    if not extract_path.exists():
        abort(404)

    return send_from_directory(extract_path, filename)


# ---- Utility helpers ----

def extract_title(md_content: str) -> str | None:
    """Return the first H1 heading text, or None."""
    for line in md_content.splitlines():
        m = re.match(r'^#\s+(.+)$', line.strip())
        if m:
            return m.group(1).strip()
    return None


def find_md_files(root: Path) -> list[Path]:
    """Return all .md files under *root*, prefer README / index."""
    md_files = list(root.rglob('*.md'))
    def _priority(p: Path) -> int:
        name = p.name.lower()
        if 'readme' in name:
            return 0
        if 'index' in name:
            return 1
        return 2
    md_files.sort(key=_priority)
    return md_files


# ---- Routes ----

@app.route('/')
def index():
    meta = load_metadata()
    return render_template('index.html', items=meta['items'])


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return 'No file part', 400

    file = request.files['file']
    if not file.filename or file.filename == '':
        return 'No file selected', 400

    if not file.filename.lower().endswith('.zip'):
        return 'Only .zip files are allowed', 400

    # Prevent path-traversal in filename
    safe_name = Path(file.filename).name
    if not safe_name:
        return 'Invalid filename', 400

    item_id = uuid.uuid4().hex[:12]

    # Persist the uploaded zip
    zip_path = UPLOAD_DIR / f'{item_id}.zip'
    file.save(zip_path)

    # Extract
    extract_path = EXTRACT_DIR / item_id
    extract_path.mkdir(exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Check for zip-bomb (simple heuristic: max 50 MB uncompressed)
            total_size = sum(info.file_size for info in zf.infolist())
            if total_size > 50 * 1024 * 1024:
                raise ValueError('Archive too large')
            zf.extractall(extract_path)
    except (zipfile.BadZipFile, ValueError, OSError) as e:
        shutil.rmtree(extract_path, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        return f'Invalid or unsafe zip file: {e}', 400

    # Locate markdown files
    md_files = find_md_files(extract_path)
    if not md_files:
        shutil.rmtree(extract_path, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        return 'No .md files found in the zip archive', 400

    main_md = md_files[0]

    # Read the main md to extract title
    try:
        with open(main_md, 'r', encoding='utf-8') as f:
            md_content = f.read()
    except (OSError, UnicodeDecodeError):
        shutil.rmtree(extract_path, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        return 'Cannot read markdown file', 400

    title = extract_title(md_content) or main_md.stem

    # Store metadata
    meta = load_metadata()
    meta['items'].insert(0, {
        'id': item_id,
        'title': title,
        'filename': safe_name,
        'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'md_file': str(main_md.relative_to(BASE_DIR)),
    })
    save_metadata(meta)

    return redirect(url_for('index') + f'?key_={ACCESS_KEY}')


@app.route('/view/<item_id>')
def view(item_id: str):
    # Basic validation: allow only hex ids
    if not re.match(r'^[0-9a-f]{12}$', item_id):
        abort(404)

    meta = load_metadata()
    item = next((it for it in meta['items'] if it['id'] == item_id), None)
    if not item:
        abort(404)

    md_path = BASE_DIR / item['md_file']
    if not md_path.exists():
        abort(404)

    with open(md_path, 'r', encoding='utf-8') as f:
        md_content = f.read()

    html_content = render_markdown(md_content, item_id=item_id)

    return render_template('view.html', item=item, content=html_content)


@app.route('/delete/<item_id>', methods=['POST'])
def delete(item_id: str):
    if not re.match(r'^[0-9a-f]{12}$', item_id):
        abort(404)

    meta = load_metadata()
    item = next((it for it in meta['items'] if it['id'] == item_id), None)

    if item:
        # Remove extracted directory
        extract_path = EXTRACT_DIR / item_id
        shutil.rmtree(extract_path, ignore_errors=True)

        # Remove uploaded zip
        zip_path = UPLOAD_DIR / f'{item_id}.zip'
        zip_path.unlink(missing_ok=True)

        # Update metadata
        meta['items'] = [it for it in meta['items'] if it['id'] != item_id]
        save_metadata(meta)

    return redirect(url_for('index') + f'?key_={ACCESS_KEY}')


# ---- Error pages ----

@app.errorhandler(403)
def forbidden(_e):
    return '<h1>403 Forbidden</h1><p>Access denied.</p>', 403


@app.errorhandler(404)
def not_found(_e):
    return '<h1>404 Not Found</h1><p>The requested resource does not exist.</p>', 404


# ---- Entry point ----

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
