"""Parse devops.txt (concatenated JSON arrays) and generate a beautiful HTML page."""
import json
import re
import html as html_mod
from pathlib import Path

RAW = Path(__file__).with_name("..") / ".." / "devops.txt"
RAW = Path("D:/Trading/newFlo/devops.txt")
OUT = Path("D:/Trading/newFlo/docs/devops.html")

# ---------- parse concatenated JSON arrays ----------
text = RAW.read_text(encoding="utf-8")
# The file has ][  between arrays – split and parse each
chunks = re.split(r'\]\s*\[', text)
sections = []
for i, chunk in enumerate(chunks):
    c = chunk.strip()
    if not c.startswith('['):
        c = '[' + c
    if not c.endswith(']'):
        c = c + ']'
    sections.append(json.loads(c))

# ---------- infer section names from content ----------
SECTION_NAMES = [
    "Databases",
    "Documentation & DR",
    "Application & Service Deployment",
    "Storage",
    "Networking",
    "Monitoring & Observability",
    "Compute (Virtual Machines)",
    "Containers & Orchestration",
    "Security",
    "CI/CD Pipelines",
    "Scripting & Automation",
    "Infrastructure as Code",
    "Work Management",
    "People & Mentoring",
]

LEVEL_COLORS = {
    "L0": "#6366f1",  # indigo
    "L1": "#0ea5e9",  # sky
    "L2": "#10b981",  # emerald
    "L3": "#f59e0b",  # amber
    "L4": "#ef4444",  # red
}

LEVEL_LABELS = {
    "L0": "Trainee",
    "L1": "Junior",
    "L2": "Middle",
    "L3": "Senior",
    "L4": "Lead",
}

def strip_html(s):
    """Remove HTML tags and decode entities."""
    if not s:
        return ""
    s = re.sub(r'<br\s*/?>', '\n', s)
    s = re.sub(r'</li>', '\n', s)
    s = re.sub(r'</p>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = html_mod.unescape(s)
    return s.strip()

def html_to_clean(s):
    """Convert HTML content to clean displayable HTML (keep lists, remove styles)."""
    if not s:
        return ""
    # Remove style attributes
    s = re.sub(r'\s*style="[^"]*"', '', s)
    # Convert to proper list formatting
    s = s.replace('<ul>', '<ul class="outcome-list">').replace('<ol>', '<ol class="outcome-list">')
    # Remove empty paragraphs
    s = re.sub(r'<p>\s*</p>', '', s)
    return s.strip()

# ---------- generate HTML ----------
cards_html = []
for sec_idx, section in enumerate(sections):
    sec_name = SECTION_NAMES[sec_idx] if sec_idx < len(SECTION_NAMES) else f"Section {sec_idx+1}"

    levels_html = []
    for level_data in section:
        lvl = level_data["JobLevelManagementLevelType"]
        lvl_name = level_data["JobLevelName"]
        color = LEVEL_COLORS.get(lvl, "#6b7280")
        jobs = level_data.get("ReviewJobs", [])

        jobs_html = []
        for job in jobs:
            name = job.get("Name", "").strip()
            desc = html_to_clean(job.get("Description", ""))
            outcomes = html_to_clean(job.get("Outcomes", ""))
            is_key = job.get("IsKey", False)
            is_new = job.get("IsNew", False)

            badges = ""
            if is_key:
                badges += '<span class="badge badge-key">KEY</span>'
            if is_new:
                badges += '<span class="badge badge-new">NEW</span>'

            outcomes_block = ""
            if outcomes:
                outcomes_block = f'''
                <div class="outcomes">
                    <div class="outcomes-title">Expected Outcomes</div>
                    <div class="outcomes-content">{outcomes}</div>
                </div>'''

            desc_block = ""
            if desc:
                desc_block = f'<div class="job-desc">{desc}</div>'

            jobs_html.append(f'''
            <div class="job-card{"  key-job" if is_key else ""}">
                <div class="job-header">
                    <div class="job-name">{name}</div>
                    <div class="badges">{badges}</div>
                </div>
                {desc_block}
                {outcomes_block}
            </div>''')

        jobs_joined = "\n".join(jobs_html)
        count = len(jobs)
        key_count = sum(1 for j in jobs if j.get("IsKey"))

        levels_html.append(f'''
        <div class="level-block">
            <div class="level-header" style="--level-color: {color}">
                <span class="level-badge" style="background: {color}">{lvl}</span>
                <span class="level-name">{lvl_name}</span>
                <span class="level-count">{count} task{"s" if count != 1 else ""}{f", {key_count} key" if key_count else ""}</span>
            </div>
            <div class="jobs-list">
                {jobs_joined}
            </div>
        </div>''')

    levels_joined = "\n".join(levels_html)

    # Icon mapping
    icons = {
        "Databases": "&#128451;",
        "Documentation & DR": "&#128196;",
        "Application & Service Deployment": "&#128640;",
        "Storage": "&#128190;",
        "Networking": "&#127760;",
        "Monitoring & Observability": "&#128200;",
        "Compute (Virtual Machines)": "&#128187;",
        "Containers & Orchestration": "&#128230;",
        "Security": "&#128274;",
        "CI/CD Pipelines": "&#9881;",
        "Scripting & Automation": "&#128221;",
        "Infrastructure as Code": "&#127959;",
        "Work Management": "&#128203;",
        "People & Mentoring": "&#129309;",
    }
    icon = icons.get(sec_name, "&#128736;")

    total_tasks = sum(len(l.get("ReviewJobs", [])) for l in section)
    total_key = sum(sum(1 for j in l.get("ReviewJobs", []) if j.get("IsKey")) for l in section)

    cards_html.append(f'''
    <section class="domain-section" id="section-{sec_idx}">
        <div class="domain-header" onclick="toggleSection({sec_idx})">
            <div class="domain-title">
                <span class="domain-icon">{icon}</span>
                <h2>{sec_name}</h2>
            </div>
            <div class="domain-meta">
                <span class="meta-pill">{total_tasks} tasks</span>
                {"<span class='meta-pill meta-key'>" + str(total_key) + " key</span>" if total_key else ""}
                <span class="chevron" id="chevron-{sec_idx}">&#9660;</span>
            </div>
        </div>
        <div class="domain-body" id="body-{sec_idx}">
            {levels_joined}
        </div>
    </section>''')

cards_joined = "\n".join(cards_html)

# Stats
total_all = sum(sum(len(l.get("ReviewJobs",[])) for l in sec) for sec in sections)
total_key_all = sum(sum(sum(1 for j in l.get("ReviewJobs",[]) if j.get("IsKey")) for l in sec) for sec in sections)
total_new = sum(sum(sum(1 for j in l.get("ReviewJobs",[]) if j.get("IsNew")) for l in sec) for sec in sections)

# Navigation
nav_items = []
for i, sec in enumerate(sections):
    sec_name = SECTION_NAMES[i] if i < len(SECTION_NAMES) else f"Section {i+1}"
    icons_map = {
        "Databases": "&#128451;",
        "Documentation & DR": "&#128196;",
        "Application & Service Deployment": "&#128640;",
        "Storage": "&#128190;",
        "Networking": "&#127760;",
        "Monitoring & Observability": "&#128200;",
        "Compute (Virtual Machines)": "&#128187;",
        "Containers & Orchestration": "&#128230;",
        "Security": "&#128274;",
        "CI/CD Pipelines": "&#9881;",
        "Scripting & Automation": "&#128221;",
        "Infrastructure as Code": "&#127959;",
        "Work Management": "&#128203;",
        "People & Mentoring": "&#129309;",
    }
    ic = icons_map.get(sec_name, "&#128736;")
    nav_items.append(f'<a href="#section-{i}" class="nav-item">{ic} {sec_name}</a>')

nav_joined = "\n".join(nav_items)

page = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DevOps Engineer — Competency Matrix</title>
<style>
:root {{
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #232733;
    --border: #2d3140;
    --text: #e4e5e9;
    --text-muted: #9ca3af;
    --accent: #6366f1;
    --radius: 12px;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    min-height: 100vh;
}}
.container {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 20px;
}}

/* ---- Header ---- */
.page-header {{
    text-align: center;
    padding: 48px 20px 32px;
    background: linear-gradient(135deg, #1e1b4b 0%, #0f1117 100%);
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
}}
.page-header h1 {{
    font-size: 2.2rem;
    font-weight: 700;
    background: linear-gradient(135deg, #818cf8, #6366f1, #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 8px;
}}
.page-header p {{
    color: var(--text-muted);
    font-size: 1.05rem;
}}

/* ---- Stats ---- */
.stats-row {{
    display: flex;
    gap: 16px;
    justify-content: center;
    flex-wrap: wrap;
    margin-top: 24px;
}}
.stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 28px;
    text-align: center;
    min-width: 140px;
}}
.stat-card .stat-num {{
    font-size: 2rem;
    font-weight: 700;
    color: var(--accent);
}}
.stat-card .stat-label {{
    font-size: 0.82rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}}

/* ---- Navigation ---- */
.nav-bar {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 16px;
    margin-bottom: 24px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(12px);
    background: rgba(26, 29, 39, 0.92);
}}
.nav-item {{
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.82rem;
    padding: 6px 12px;
    border-radius: 8px;
    transition: all 0.2s;
    white-space: nowrap;
}}
.nav-item:hover {{
    background: var(--surface2);
    color: var(--text);
}}

/* ---- Filter ---- */
.filter-bar {{
    display: flex;
    gap: 8px;
    margin-bottom: 24px;
    flex-wrap: wrap;
    align-items: center;
}}
.filter-bar label {{
    font-size: 0.85rem;
    color: var(--text-muted);
    margin-right: 4px;
}}
.filter-btn {{
    padding: 6px 16px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    font-size: 0.82rem;
    transition: all 0.2s;
}}
.filter-btn:hover, .filter-btn.active {{
    background: var(--accent);
    color: white;
    border-color: var(--accent);
}}
.search-box {{
    flex: 1;
    min-width: 200px;
    padding: 8px 16px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 0.9rem;
    outline: none;
    transition: border-color 0.2s;
}}
.search-box:focus {{
    border-color: var(--accent);
}}
.search-box::placeholder {{
    color: var(--text-muted);
}}

/* ---- Sections ---- */
.domain-section {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 16px;
    overflow: hidden;
    transition: box-shadow 0.2s;
}}
.domain-section:hover {{
    box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.3);
}}
.domain-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 18px 24px;
    cursor: pointer;
    user-select: none;
    transition: background 0.15s;
}}
.domain-header:hover {{
    background: var(--surface2);
}}
.domain-title {{
    display: flex;
    align-items: center;
    gap: 12px;
}}
.domain-icon {{
    font-size: 1.5rem;
}}
.domain-title h2 {{
    font-size: 1.15rem;
    font-weight: 600;
}}
.domain-meta {{
    display: flex;
    align-items: center;
    gap: 8px;
}}
.meta-pill {{
    font-size: 0.75rem;
    padding: 3px 10px;
    border-radius: 12px;
    background: var(--surface2);
    color: var(--text-muted);
}}
.meta-key {{
    background: rgba(245, 158, 11, 0.15);
    color: #f59e0b;
}}
.chevron {{
    font-size: 0.8rem;
    color: var(--text-muted);
    transition: transform 0.3s;
    margin-left: 8px;
}}
.chevron.collapsed {{
    transform: rotate(-90deg);
}}
.domain-body {{
    padding: 0 24px 20px;
    transition: max-height 0.3s ease;
}}
.domain-body.hidden {{
    display: none;
}}

/* ---- Level blocks ---- */
.level-block {{
    margin-bottom: 20px;
}}
.level-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
    margin-bottom: 8px;
    border-bottom: 1px solid var(--border);
}}
.level-badge {{
    font-size: 0.7rem;
    font-weight: 700;
    padding: 3px 10px;
    border-radius: 6px;
    color: white;
    letter-spacing: 0.05em;
}}
.level-name {{
    font-weight: 600;
    font-size: 0.95rem;
}}
.level-count {{
    font-size: 0.78rem;
    color: var(--text-muted);
    margin-left: auto;
}}

/* ---- Job cards ---- */
.jobs-list {{
    display: flex;
    flex-direction: column;
    gap: 10px;
}}
.job-card {{
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    transition: all 0.2s;
}}
.job-card:hover {{
    border-color: rgba(99, 102, 241, 0.4);
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(0,0,0,0.2);
}}
.job-card.key-job {{
    border-left: 3px solid #f59e0b;
}}
.job-header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    margin-bottom: 6px;
}}
.job-name {{
    font-weight: 600;
    font-size: 0.95rem;
    color: #c7d2fe;
}}
.badges {{
    display: flex;
    gap: 4px;
    flex-shrink: 0;
}}
.badge {{
    font-size: 0.65rem;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 4px;
    letter-spacing: 0.05em;
}}
.badge-key {{
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
}}
.badge-new {{
    background: rgba(16, 185, 129, 0.2);
    color: #34d399;
}}
.job-desc {{
    font-size: 0.85rem;
    color: var(--text-muted);
    margin-bottom: 8px;
    line-height: 1.5;
}}
.job-desc p {{
    margin-bottom: 4px;
}}

/* ---- Outcomes ---- */
.outcomes {{
    margin-top: 10px;
    padding: 12px 16px;
    background: rgba(99, 102, 241, 0.06);
    border-radius: 8px;
    border: 1px solid rgba(99, 102, 241, 0.12);
}}
.outcomes-title {{
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #818cf8;
    margin-bottom: 6px;
}}
.outcomes-content {{
    font-size: 0.83rem;
    color: var(--text-muted);
    line-height: 1.55;
}}
.outcomes-content p {{
    margin-bottom: 4px;
}}
.outcomes-content ul, .outcomes-content ol {{
    padding-left: 20px;
    margin: 4px 0;
}}
.outcomes-content li {{
    margin-bottom: 3px;
}}

/* ---- Responsive ---- */
@media (max-width: 768px) {{
    .page-header h1 {{ font-size: 1.5rem; }}
    .stats-row {{ gap: 8px; }}
    .stat-card {{ min-width: 100px; padding: 12px 16px; }}
    .stat-card .stat-num {{ font-size: 1.5rem; }}
    .domain-header {{ padding: 14px 16px; }}
    .domain-body {{ padding: 0 16px 16px; }}
    .nav-bar {{ padding: 8px 10px; }}
    .container {{ padding: 12px; }}
}}

/* ---- Scrollbar ---- */
::-webkit-scrollbar {{ width: 8px; }}
::-webkit-scrollbar-track {{ background: var(--bg); }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: #3d4155; }}

/* ---- Print ---- */
@media print {{
    body {{ background: white; color: black; }}
    .nav-bar, .filter-bar {{ display: none; }}
    .domain-body.hidden {{ display: block !important; }}
    .job-card {{ break-inside: avoid; }}
}}
</style>
</head>
<body>

<div class="page-header">
    <h1>DevOps Engineer — Competency Matrix</h1>
    <p>Career progression from Trainee (L0) to Lead (L4) across {len(sections)} competency domains</p>
    <div class="stats-row">
        <div class="stat-card">
            <div class="stat-num">{len(sections)}</div>
            <div class="stat-label">Domains</div>
        </div>
        <div class="stat-card">
            <div class="stat-num">{total_all}</div>
            <div class="stat-label">Total Tasks</div>
        </div>
        <div class="stat-card">
            <div class="stat-num">{total_key_all}</div>
            <div class="stat-label">Key Tasks</div>
        </div>
        <div class="stat-card">
            <div class="stat-num">{total_new}</div>
            <div class="stat-label">New Tasks</div>
        </div>
    </div>
</div>

<div class="container">

<nav class="nav-bar">
    {nav_joined}
</nav>

<div class="filter-bar">
    <input type="text" class="search-box" id="searchBox" placeholder="Search tasks..." oninput="filterTasks()">
    <label>Level:</label>
    <button class="filter-btn active" onclick="filterLevel(this, 'all')">All</button>
    <button class="filter-btn" onclick="filterLevel(this, 'L0')">L0</button>
    <button class="filter-btn" onclick="filterLevel(this, 'L1')">L1</button>
    <button class="filter-btn" onclick="filterLevel(this, 'L2')">L2</button>
    <button class="filter-btn" onclick="filterLevel(this, 'L3')">L3</button>
    <button class="filter-btn" onclick="filterLevel(this, 'L4')">L4</button>
    <button class="filter-btn" onclick="filterKey(this)" id="keyFilter">Key Only</button>
</div>

{cards_joined}

</div>

<script>
function toggleSection(idx) {{
    const body = document.getElementById('body-' + idx);
    const chev = document.getElementById('chevron-' + idx);
    body.classList.toggle('hidden');
    chev.classList.toggle('collapsed');
}}

let activeLevel = 'all';
let keyOnly = false;

function filterLevel(btn, level) {{
    activeLevel = level;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    if (keyOnly) document.getElementById('keyFilter').classList.add('active');
    applyFilters();
}}

function filterKey(btn) {{
    keyOnly = !keyOnly;
    btn.classList.toggle('active');
    applyFilters();
}}

function filterTasks() {{
    applyFilters();
}}

function applyFilters() {{
    const q = document.getElementById('searchBox').value.toLowerCase();
    document.querySelectorAll('.level-block').forEach(block => {{
        const badge = block.querySelector('.level-badge');
        const lvl = badge ? badge.textContent.trim() : '';
        const matchLevel = activeLevel === 'all' || lvl === activeLevel;

        if (!matchLevel) {{
            block.style.display = 'none';
            return;
        }}

        let anyVisible = false;
        block.querySelectorAll('.job-card').forEach(card => {{
            const text = card.textContent.toLowerCase();
            const isKey = card.classList.contains('key-job');
            const matchSearch = !q || text.includes(q);
            const matchKey = !keyOnly || isKey;

            if (matchSearch && matchKey) {{
                card.style.display = '';
                anyVisible = true;
            }} else {{
                card.style.display = 'none';
            }}
        }});

        block.style.display = anyVisible ? '' : 'none';
    }});
}}

// Start with all sections expanded
</script>

</body>
</html>'''

OUT.write_text(page, encoding="utf-8")
print(f"Generated {OUT} ({len(page)} bytes)")
