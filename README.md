# MD Displayer

A Flask-based Markdown document viewer with multi-theme support. Upload a `.zip` file containing Markdown documents, and it extracts and renders them beautifully with syntax highlighting, LaTeX math (KaTeX), and GFM task lists.

## Features

- **Upload & Extract** — Drag-and-drop a `.zip` containing `.md` files; auto-extracts and picks the main document
- **Markdown Rendering** — Powered by Python-Markdown + PyMdown Extensions (tables, fenced code, math, task lists, etc.)
- **7 Themes** — Dark, Light, Forest, Sunset, Ocean, Mono, Cyberpunk
- **LaTeX Math** — KaTeX rendering with CDN fallback chain (bootcdn → jsdelivr → unpkg)
- **Code Highlighting** — Pygments-based syntax highlighting
- **Access Control** — Simple key-based gate (`?key_=display`)
- **Persistence** — Uploaded files and metadata survive restarts

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python app.py
```

Then open `http://localhost:5000/?key_=display`

## Requirements

- Python 3.10+
- Flask ≥ 3.0
- Python-Markdown ≥ 3.7
- PyMdown Extensions ≥ 10.14
- Bleach ≥ 6.2

## License

MIT
