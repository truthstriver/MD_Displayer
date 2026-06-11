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


# Regex to match display-math blocks delimited by $$ (single-line or multi-line)
_DISPLAY_MATH_RE = re.compile(r'\$\$(.+?)\$\$', re.DOTALL)


def _normalize_display_math(text: str) -> str:
    """
    Ensure $$...$$ display-math blocks are surrounded by blank lines so that
    smart_dollar can recognise them as block-level math (not inline paragraphs).
    """
    def _fix(m: re.Match) -> str:
        block = m.group(0)
        start, end = m.start(), m.end()

        # Check if preceded by \n\n (or at start of text)
        if start > 0 and text[start-2:start] != '\n\n':
            # Also handle single \n (not blank line)
            if text[start-1:start] == '\n':
                block = '\n' + block
            else:
                block = '\n\n' + block

        # Check if followed by \n\n (or at end of text)
        if end < len(text) and text[end:end+2] != '\n\n':
            if text[end:end+1] == '\n':
                block = block + '\n'
            else:
                block = block + '\n\n'

        return block

    return _DISPLAY_MATH_RE.sub(_fix, text)


def render_markdown(text: str, item_id: str = '') -> str:
    """
    Convert markdown text to HTML using Python-Markdown + pymdown-extensions.

    Args:
        text: Raw markdown content.
        item_id: The item's extraction directory ID (for rewriting image URLs).
    """
    # Pre-process: normalise blank lines around $$ display-math blocks
    text = _normalize_display_math(text)

    md = _make_md()
    raw_html = md.convert(text)

    # Sanitize the HTML (defence-in-depth against XSS)
    clean_html = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        css_sanitizer=CSSSanitizer(),
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
