import streamlit as st
import os
import re
import git
import tempfile
import hashlib
import shutil
import csv
import io
import pandas as pd

# --- 1. SETUP & CONSTANTS ---
st.set_page_config(page_title="Alation Docs Segregator & Exporter", layout="wide")
st.title("Alation Docs Segregator (With Migration Exporter)")

REPO_URL = st.secrets.get("REPO_URL", "github.com/your-org/your-repo.git")

if "your-org/your-repo" in REPO_URL:
    st.error("🚨 Configuration Error: Please update your Streamlit Secrets with your actual repository URL.")
    st.stop()

# Exact strings to search for
LABEL_BOTH = ".. include:: /shared/ProductLabels/CloudAndCustomerManaged_Label.rst"
LABEL_CLOUD = ".. include:: /shared/ProductLabels/Cloud_Label.rst"
LABEL_ONPREM = ".. include:: /shared/ProductLabels/CustomerManaged_Label.rst"

# Technical directories to completely ignore
IGNORE_DIRS = {'_build', '.github', 'venv', 'env', '.git', '__pycache__', 'node_modules'}
ESSENTIAL_BUILD_FILES = {'conf.py', 'makefile', 'make.bat', 'requirements.txt'}

# --- REGEX COMPILES FOR DEPENDENCY TRACING ---
RE_LABEL_DEF = re.compile(r'^\s*\.\.\s+_([^:]+):', re.MULTILINE)
RE_INCLUDE = re.compile(r'^\s*\.\.\s+include::\s+(.+)$', re.MULTILINE)
RE_IMAGE = re.compile(r'\.\.\s+(?:\|[^\|]+\|\s+)?(?:image|figure)::\s+([^\s]+)', re.MULTILINE)
RE_DOC = re.compile(r':doc:`(?:[^<`]*<([^>]+)>|([^`]+))`')
RE_REF = re.compile(r':ref:`(?:[^<`]*<([^>]+)>|([^`]+))`')
RE_DOWNLOAD = re.compile(r':download:`(?:[^<`]*<([^>]+)>|([^`]+))`')
RE_MERMAID = re.compile(r'\.\.\s+mermaid::\s+([^\s]+\.mmd)', re.MULTILINE)
RE_VIDEO = re.compile(r'\.\.\s+video::\s+([^\s]+)', re.MULTILINE)
# Grid table detector: matches +---+---+ or +===+===+
RE_GRID_TABLE = re.compile(r'^\+[-=]+\+[-=]+\+', re.MULTILINE) 

# --- 2. PATH RESOLUTION HELPER ---
def resolve_sphinx_path(current_file_rel, ref_path):
    ref_path = ref_path.strip()
    if ref_path.startswith('/'):
        return ref_path.lstrip('/')
    current_dir = os.path.dirname(current_file_rel)
    resolved = os.path.normpath(os.path.join(current_dir, ref_path))
    return resolved.replace('\\', '/')

# --- 3. CORE DEPENDENCY & BFS PROPAGATION ---
def propagate_tags(start_files, target_set, file_dependencies):
    queue = list(start_files)
    while queue:
        curr = queue.pop(0)
        if curr in target_set: 
            continue
        target_set.add(curr)
        for dep in file_dependencies.get(curr, []):
            queue.append(dep)

# --- 4. PHASE 1: ANALYSIS ---
def analyze_dependencies(repo_dir):
    file_tags = {}           
    label_to_file = {}       
    file_dependencies = {}   
    all_files = set()
    grid_table_files = set()
    
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
        for file in files:
            if file.endswith('.rst') or file.endswith('.mmd') or file.endswith('.mp4'):
                file_path = os.path.relpath(os.path.join(root, file), repo_dir).replace("\\", "/")
                all_files.add(file_path)
                
                if file.endswith('.rst'):
                    with open(os.path.join(repo_dir, file_path), 'r', encoding='utf-8') as f:
                        content = f.read()
                        
                    tags = set()
                    if LABEL_CLOUD in content: tags.add('Alation Cloud Service')
                    if LABEL_ONPREM in content: tags.add('CustomerManaged')
                    if LABEL_BOTH in content:
                        tags.add('Alation Cloud Service')
                        tags.add('CustomerManaged')
                    file_tags[file_path] = tags
                    
                    if RE_GRID_TABLE.search(content):
                        grid_table_files.add(file_path)
                    
                    for match in RE_LABEL_DEF.finditer(content):
                        label_to_file[match.group(1).strip()] = file_path

    for file_path in file_tags.keys():
        deps = set()
        with open(os.path.join(repo_dir, file_path), 'r', encoding='utf-8') as f:
            content = f.read()
            
        for match in RE_INCLUDE.finditer(content): deps.add(resolve_sphinx_path(file_path, match.group(1)))
        for match in RE_IMAGE.finditer(content): deps.add(resolve_sphinx_path(file_path, match.group(1)))
        for match in RE_MERMAID.finditer(content): deps.add(resolve_sphinx_path(file_path, match.group(1)))
        for match in RE_VIDEO.finditer(content): deps.add(resolve_sphinx_path(file_path, match.group(1)))
        for match in RE_DOWNLOAD.finditer(content):
            ref = match.group(1) if match.group(1) else match.group(2)
            deps.add(resolve_sphinx_path(file_path, ref))
        for match in RE_DOC.finditer(content):
            ref = match.group(1) if match.group(1) else match.group(2)
            resolved = resolve_sphinx_path(file_path, ref)
            if not resolved.endswith('.rst') and '.' not in os.path.basename(resolved):
                resolved += '.rst'
            deps.add(resolved)
        for match in RE_REF.finditer(content):
            label = match.group(1) if match.group(1) else match.group(2)
            if label.strip() in label_to_file:
                deps.add(label_to_file[label.strip()])
                
        file_dependencies[file_path] = deps

    cloud_required = set()
    onprem_required = set()

    cloud_starts = [p for p, t in file_tags.items() if 'Alation Cloud Service' in t]
    onprem_starts = [p for p, t in file_tags.items() if 'CustomerManaged' in t]

    propagate_tags(cloud_starts, cloud_required, file_dependencies)
    propagate_tags(onprem_starts, onprem_required, file_dependencies)

    untagged = []
    for f in all_files:
        if f.lower() not in ESSENTIAL_BUILD_FILES and f not in cloud_required and f not in onprem_required:
            untagged.append(f)
            
    return cloud_required, onprem_required, untagged, file_dependencies, grid_table_files

# --- 5. PHASE 2: TRANSLATOR ENGINE ---
def convert_rst_to_md(content, mode, current_rel_path, repo_dir, target_base_dir):
    """Translates reST to Markdown handling Alation Sphinx corner cases."""
    
    # 1. Global Epilog Substitutions
    content = content.replace('|v|', '✅')
    content = content.replace('|x|', '❌')

    # 2. Strip LaTeX-only directives, table formatting, and classes
    content = re.sub(r'^\s*\.\.\s+tabularcolumns::.*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'\.\.\s+only::\s*latex\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', '', content)
    content = re.sub(r'^\s*\.\.\s+rst-class::.*$', '', content, flags=re.MULTILINE)

    # 3. Unwrap HTML-only blocks and Containers
    def unwrap_block(match):
        return "\n" + re.sub(r'^[ \t]+', '', match.group(1), flags=re.MULTILINE) + "\n"
    content = re.sub(r'\.\.\s+(?:only::\s*html|container::)\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', unwrap_block, content)

    # 4. Meta to YAML Frontmatter
    def handle_meta(match):
        yaml_lines = ["---"]
        for line in match.group(1).split('\n'):
            line = line.strip()
            if line.startswith(':'):
                parts = line.split(':', 2)
                if len(parts) >= 3:
                    key, val = parts[1].strip(), parts[2].strip()
                    yaml_lines.append(f'{key}: "{val}"' if ':' in val else f"{key}: {val}")
        yaml_lines.append("---")
        return "\n".join(yaml_lines) + "\n\n"
    content = re.sub(r'^\s*\.\.\s+meta::\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_meta, content, flags=re.MULTILINE)

    # 5. Headers (including ^^^ level 4 headers)
    content = re.sub(r'^([^\n]+)\n[=]{3,}$', r'# \1', content, flags=re.MULTILINE)
    content = re.sub(r'^([^\n]+)\n[-]{3,}$', r'## \1', content, flags=re.MULTILINE)
    content = re.sub(r'^([^\n]+)\n[~]{3,}$', r'### \1', content, flags=re.MULTILINE)
    content = re.sub(r'^([^\n]+)\n[\^]{3,}$', r'#### \1', content, flags=re.MULTILINE)

    # 6. Links (:doc:, :ref:)
    content = re.sub(r':doc:`(?:[^<`]*<([^>]+)>|([^`]+))`', lambda m: f"[{m.group(1) or m.group(2)}]({(m.group(1) or m.group(2)).replace('.rst', '')}.md)", content)
    content = re.sub(r':ref:`(?:[^<`]*<([^>]+)>|([^`]+))`', lambda m: f"[{m.group(1) or m.group(2)}](#{(m.group(1) or m.group(2)).lower().replace(' ', '-')})", content)

    # 7. Code Blocks
    def replace_code_block(match):
        lang, code = match.group(1), match.group(2)
        return f"```{lang}\n{re.sub(r'^[ \t]+', '', code, flags=re.MULTILINE)}\n```"
    content = re.sub(r'\.\.\s+code-block::\s*(\w*)\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', replace_code_block, content)

    # 8. Raw HTML Directives
    def handle_raw_html(match):
        return f"\n{re.sub(r'^[ \t]+', '', match.group(1), flags=re.MULTILINE)}\n"
    content = re.sub(r'\.\.\s+raw::\s*html\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_raw_html, content)

    # 9. Includes with FAIL-SAFE logic
    def handle_include(match):
        include_path = match.group(1).strip()
        resolved_original = resolve_sphinx_path(current_rel_path, include_path)
        
        if mode == 'flat' or not os.path.exists(os.path.join(target_base_dir, resolved_original)):
            try:
                with open(os.path.join(repo_dir, resolved_original), 'r', encoding='utf-8') as f:
                    return "\n\n" + convert_rst_to_md(f.read(), mode, resolved_original, repo_dir, target_base_dir) + "\n\n"
            except Exception:
                return f"> **Error:** Fail-safe missed include: {include_path}"
                
        if mode == 'mintlify': return f'<Snippet file="{include_path.lstrip("/").replace(".rst", ".mdx")}" />'
        elif mode == 'gitbook': return f'{{% include "{include_path.replace(".rst", ".md")}" %}}'
            
    content = re.sub(r'^\s*\.\.\s+include::\s+(.+)$', handle_include, content, flags=re.MULTILINE)

    # 10. Admonitions (Strips Sphinx attributes like :name:)
    def handle_admonition(match):
        adm_type, text = match.group(1).title(), match.group(2)
        text = re.sub(r'^[ \t]*:[a-zA-Z_-]+:.*$\n', '', text, flags=re.MULTILINE)
        unindented = re.sub(r'^[ \t]+', '', text, flags=re.MULTILINE).strip()
        
        if mode == 'mintlify': return f"<{adm_type}>\n{unindented}\n</{adm_type}>"
        return f"> **{adm_type}**\n> {unindented.replace(chr(10), chr(10) + '> ')}"
    content = re.sub(r'\.\.\s+(note|warning|tip|important|caution|info)::\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_admonition, content)

    # 11. Media (Mermaid, Video)
    content = re.sub(r'^\s*\.\.\s+mermaid::\s+([^\s]+\.mmd)', lambda m: f"```mermaid\n{open(os.path.join(repo_dir, resolve_sphinx_path(current_rel_path, m.group(1).strip())), 'r', encoding='utf-8').read().strip()}\n```" if os.path.exists(os.path.join(repo_dir, resolve_sphinx_path(current_rel_path, m.group(1).strip()))) else "", content, flags=re.MULTILINE)
    content = re.sub(r'^\s*\.\.\s+video::\s+([^\s]+)', lambda m: f'<video controls width="100%"><source src="{m.group(1).strip().lstrip("/")}" type="video/mp4"></video>', content, flags=re.MULTILINE)

    # 12. UI Components (Collapse, Tabs)
    content = re.sub(r'\.\.\s+collapse::\s*(.*?)\n+((?:(?:[ \t]+)[^\n]*\n?)+)', lambda m: f"<details>\n<summary>{m.group(1).strip()}</summary>\n\n{re.sub(r'^[ \t]+', '', m.group(2), flags=re.MULTILINE).strip()}\n</details>", content)
    content = re.sub(r'^\s*\.\.\s+tabs::\s*\n', '', content, flags=re.MULTILINE)
    content = re.sub(r'\.\.\s+tab::\s*(.*?)\n+((?:(?:[ \t]+)[^\n]*\n?)+)', lambda m: f"{{% tab title=\"{m.group(1).strip()}\" %}}\n{re.sub(r'^[ \t]+', '', m.group(2), flags=re.MULTILINE).strip()}\n{{% endtab %}}" if mode == 'gitbook' else f"### {m.group(1).strip()}\n\n{re.sub(r'^[ \t]+', '', m.group(2), flags=re.MULTILINE).strip()}\n", content)

    # 13. Basic formatting
    content = re.sub(r'``([^`]+)``', r'`\1`', content)
    
    return content

# --- 6. PHASE 3: FILE GENERATION & ZIP ---
def generate_segregated_environment(repo_dir, cloud_required, onprem_required, output_mode):
    staging_dir = os.path.join(tempfile.gettempdir(), f"segregated_docs_{os.urandom(4).hex()}")
    cloud_dir = os.path.join(staging_dir, "Alation Cloud Service")
    onprem_dir = os.path.join(staging_dir, "CustomerManaged")
    
    stats = {"cloud": 0, "onprem": 0}

    def safe_copy(src_rel_path, target_base_dir):
        src_abs = os.path.join(repo_dir, src_rel_path)
        if os.path.exists(src_abs):
            target_abs = os.path.join(target_base_dir, src_rel_path)
            os.makedirs(os.path.dirname(target_abs), exist_ok=True)
            shutil.copy2(src_abs, target_abs)

    # 1. Standard Segregation Copy (For all modes)
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
        for file in files:
            rel_path = os.path.relpath(os.path.join(root, file), repo_dir).replace("\\", "/")
            
            if file.lower() in ESSENTIAL_BUILD_FILES and output_mode == 'rest':
                safe_copy(rel_path, cloud_dir)
                safe_copy(rel_path, onprem_dir)
                continue

            if rel_path in cloud_required:
                safe_copy(rel_path, cloud_dir)
                stats["cloud"] += 1
            if rel_path in onprem_required:
                safe_copy(rel_path, onprem_dir)
                stats["onprem"] += 1

    # 2. Translation Execution (Only if Markdown is selected)
    if output_mode != 'rest':
        for target_env in [cloud_dir, onprem_dir]:
            if not os.path.exists(target_env): continue
            
            for root, _, files in os.walk(target_env):
                for file in files:
                    if file.endswith('.rst'):
                        file_path = os.path.join(root, file)
                        rel_repo_path = os.path.relpath(file_path, target_env).replace("\\", "/")
                        
                        with open(file_path, 'r', encoding='utf-8') as f:
                            raw_content = f.read()
                            
                        translated_content = convert_rst_to_md(raw_content, output_mode, rel_repo_path, repo_dir, target_env)
                        
                        ext = '.mdx' if output_mode == 'mintlify' else '.md'
                        new_file_path = file_path[:-4] + ext
                        
                        with open(new_file_path, 'w', encoding='utf-8') as f:
                            f.write(translated_content)
                            
                        os.remove(file_path) # Clean up old .rst

    # 3. Zip and Cleanup
    zip_base_path = os.path.join(tempfile.gettempdir(), "Alation_Final_Docs")
    zip_filepath = shutil.make_archive(zip_base_path, 'zip', staging_dir)
    shutil.rmtree(staging_dir, ignore_errors=True)
    
    return zip_filepath, stats

# --- 7. UI WORKFLOW ---
def main():
    with st.sidebar:
        st.header("🔑 Credentials Setup")
        github_pat = st.text_input("GitHub PAT", type="password")
        
        if st.button("🚪 Logout & Clean Workspace", type="primary"):
            st.session_state.clear()
            st.rerun()

    if not github_pat:
        st.warning("👈 Please enter your GitHub PAT in the sidebar to clone the repository.")
        st.stop()
        
    user_hash = hashlib.md5(github_pat.encode()).hexdigest()[:8]
    REPO_DIR = os.path.join(tempfile.gettempdir(), f"docs_repo_{user_hash}")

    st.write("### 1. Repository Setup")
    if st.button("⬇️ Clone / Pull Latest Docs Repository"):
        with st.spinner("Fetching repository data..."):
            try:
                auth_url = f"https://oauth2:{github_pat}@{REPO_URL}"
                if not os.path.exists(os.path.join(REPO_DIR, ".git")):
                    git.Repo.clone_from(auth_url, REPO_DIR)
                    st.success("Repository cloned successfully!")
                else:
                    repo = git.Repo(REPO_DIR)
                    repo.remotes.origin.set_url(auth_url)
                    repo.remotes.origin.pull()
                    st.success("Repository pulled and is up to date!")
                st.session_state['repo_ready'] = True
            except Exception as e:
                st.error(f"Failed to fetch repository: {e}")

    if st.session_state.get('repo_ready', False) or os.path.exists(os.path.join(REPO_DIR, ".git")):
        st.divider()
        st.write("### 2. Analyze Dependency Graph")
        
        if st.button("🔍 Scan for Tags & Dependencies"):
            with st.spinner("Mapping dependency graph..."):
                c_req, o_req, untagged, deps, grid_tables = analyze_dependencies(REPO_DIR)
                
                st.session_state['cloud_req'] = c_req
                st.session_state['onprem_req'] = o_req
                st.session_state['deps'] = deps
                st.session_state['grid_tables'] = grid_tables
                
                df = pd.DataFrame({"File Path": sorted(untagged), "Action": ["Ignore"] * len(untagged)})
                st.session_state['untagged_df'] = df
                st.success(f"Found {len(c_req)} Alation Cloud Service files, {len(o_req)} CustomerManaged files, and {len(untagged)} Untagged files.")

        if 'untagged_df' in st.session_state:
            st.divider()
            st.write("### 3. Review Untagged / Orphaned Files")
            st.info("Assign environments to orphaned files below. Leave as 'Ignore' to skip them.")
            
            edited_df = st.data_editor(
                st.session_state['untagged_df'],
                column_config={
                    "Action": st.column_config.SelectboxColumn("Action", options=["Ignore", "Alation Cloud Service", "CustomerManaged", "Both"], required=True),
                    "File Path": st.column_config.TextColumn(disabled=True)
                },
                use_container_width=True, hide_index=True
            )

            st.download_button("📄 Download Untagged Report (CSV)", data=st.session_state['untagged_df'].to_csv(index=False).encode('utf-8'), file_name="untagged_report.csv", mime="text/csv")

            st.divider()
            st.write("### 4. Format Selection & Build")
            
            output_format = st.radio(
                "Select Output Architecture:", 
                options=["Sphinx reST (Original)", "Mintlify (MDX)", "GitBook (MD)", "Flat Markdown (MD)"],
                help="Select your target migration tool. If flat is selected or references are missing, content will be safely inlined."
            )

            # Warning for Grid Tables if migrating to Markdown
            if output_format != "Sphinx reST (Original)" and st.session_state.get('grid_tables'):
                st.warning(f"⚠️ **Warning:** Found {len(st.session_state['grid_tables'])} files containing Sphinx Grid Tables. Standard Markdown does not support grid tables. These will render as raw text and require manual formatting in your target platform.")
                with st.expander("View files with Grid Tables"):
                    st.write(list(st.session_state['grid_tables']))

            if st.button("🚀 Apply Manual Tags & Generate ZIP", type="primary"):
                with st.spinner("Applying rules, generating output, and zipping files..."):
                    final_cloud = set(st.session_state['cloud_req'])
                    final_onprem = set(st.session_state['onprem_req'])
                    deps = st.session_state['deps']
                    
                    manual_cloud_starts = []
                    manual_onprem_starts = []
                    
                    for _, row in edited_df.iterrows():
                        action = row['Action']
                        path = row['File Path']
                        if action in ["Alation Cloud Service", "Both"]: manual_cloud_starts.append(path)
                        if action in ["CustomerManaged", "Both"]: manual_onprem_starts.append(path)
                            
                    propagate_tags(manual_cloud_starts, final_cloud, deps)
                    propagate_tags(manual_onprem_starts, final_onprem, deps)
                    
                    mode_map = {"Sphinx reST (Original)": "rest", "Mintlify (MDX)": "mintlify", "GitBook (MD)": "gitbook", "Flat Markdown (MD)": "flat"}
                    selected_mode = mode_map[output_format]

                    zip_path, stats = generate_segregated_environment(REPO_DIR, final_cloud, final_onprem, selected_mode)
                    
                    st.success("Successfully generated environment ZIP!")
                    col1, col2 = st.columns(2)
                    col1.metric("Total Alation Cloud Service Files", stats["cloud"])
                    col2.metric("Total CustomerManaged Files", stats["onprem"])
                    
                    with open(zip_path, "rb") as fp:
                        st.download_button(
                            label="📦 Download Final Output (ZIP)",
                            data=fp, file_name="Alation_Docs_Output.zip", mime="application/zip",
                            type="primary", use_container_width=True
                        )

if __name__ == "__main__":
    main()
