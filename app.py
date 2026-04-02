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
import google.generativeai as genai
import gc  # Added for aggressive memory management

# --- 1. SETUP & CONSTANTS ---
st.set_page_config(page_title="Alation Docs Segregator & AI Exporter", layout="wide")
st.title("Alation Docs Segregator (With Migration & AI Exporter)")

REPO_URL = st.secrets.get("REPO_URL", "github.com/your-org/your-repo.git")

if "your-org/your-repo" in REPO_URL:
    st.error("🚨 Configuration Error: Please update your Streamlit Secrets with your actual repository URL.")
    st.stop()

# Explicit Context Files for AI
GLOSSARY_PATH = "welcome/Glossary/index.rst"
ROLES_PATH = "welcome/CatalogBasics/RolesOverview.rst"

LABEL_BOTH = ".. include:: /shared/ProductLabels/CloudAndCustomerManaged_Label.rst"
LABEL_CLOUD = ".. include:: /shared/ProductLabels/Cloud_Label.rst"
LABEL_ONPREM = ".. include:: /shared/ProductLabels/CustomerManaged_Label.rst"

IGNORE_DIRS = {'_build', '.github', 'venv', 'env', '.git', '__pycache__', 'node_modules'}
ESSENTIAL_BUILD_FILES = {'conf.py', 'makefile', 'make.bat', 'requirements.txt'}

# --- REGEX COMPILES ---
RE_LABEL_DEF = re.compile(r'^\s*\.\.\s+_([^:]+):', re.MULTILINE)
RE_INCLUDE = re.compile(r'^\s*\.\.\s+include::\s+(.+)$', re.MULTILINE)
RE_IMAGE = re.compile(r'\.\.\s+(?:\|[^\|]+\|\s+)?(?:image|figure)::\s+([^\s]+)', re.MULTILINE)
RE_DOC = re.compile(r':doc:`(?:[^<`]*<([^>]+)>|([^`]+))`')
RE_REF = re.compile(r':ref:`(?:[^<`]*<([^>]+)>|([^`]+))`')
RE_DOWNLOAD = re.compile(r':download:`(?:[^<`]*<([^>]+)>|([^`]+))`')
RE_MERMAID = re.compile(r'\.\.\s+mermaid::\s+([^\s]+\.mmd)', re.MULTILINE)
RE_VIDEO = re.compile(r'\.\.\s+video::\s+([^\s]+)', re.MULTILINE)
RE_GRID_TABLE = re.compile(r'^\+[-=]+\+[-=]+\+', re.MULTILINE)
RE_USER_ROLE = re.compile(r'^\s*:user_role:\s*(.+)$', re.MULTILINE)

# --- 2. HELPER FUNCTIONS ---
def resolve_sphinx_path(current_file_rel, ref_path):
    ref_path = ref_path.strip()
    if ref_path.startswith('/'): return ref_path.lstrip('/')
    return os.path.normpath(os.path.join(os.path.dirname(current_file_rel), ref_path)).replace('\\', '/')

def propagate_tags(start_files, target_set, file_dependencies):
    queue = list(start_files)
    while queue:
        curr = queue.pop(0)
        if curr in target_set: continue
        target_set.add(curr)
        queue.extend(file_dependencies.get(curr, []))

# --- 3. PHASE 1: ANALYSIS ---
def analyze_dependencies(repo_dir):
    file_tags, label_to_file, file_dependencies = {}, {}, {}
    all_files, grid_table_files = set(), set()
    file_roles = {}
    
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
        for file in files:
            if file.endswith(('.rst', '.mmd', '.mp4')):
                file_path = os.path.relpath(os.path.join(root, file), repo_dir).replace("\\", "/")
                all_files.add(file_path)
                
                if file.endswith('.rst'):
                    with open(os.path.join(repo_dir, file_path), 'r', encoding='utf-8') as f:
                        content = f.read()
                        
                    tags = set()
                    if LABEL_CLOUD in content or LABEL_BOTH in content: tags.add('Cloud')
                    if LABEL_ONPREM in content or LABEL_BOTH in content: tags.add('OnPrem')
                    file_tags[file_path] = tags
                    
                    role_match = RE_USER_ROLE.search(content)
                    if role_match:
                        role_str = role_match.group(1).lower()
                        file_roles[file_path] = 'Admin' if 'admin' in role_str else 'User'
                    else:
                        file_roles[file_path] = 'User'
                    
                    if RE_GRID_TABLE.search(content): grid_table_files.add(file_path)
                    for match in RE_LABEL_DEF.finditer(content): label_to_file[match.group(1).strip()] = file_path

    for file_path in file_tags.keys():
        deps = set()
        with open(os.path.join(repo_dir, file_path), 'r', encoding='utf-8') as f: content = f.read()
        for regex in [RE_INCLUDE, RE_IMAGE, RE_MERMAID, RE_VIDEO]:
            for match in regex.finditer(content): deps.add(resolve_sphinx_path(file_path, match.group(1)))
        for match in RE_DOWNLOAD.finditer(content):
            deps.add(resolve_sphinx_path(file_path, match.group(1) or match.group(2)))
        for match in RE_DOC.finditer(content):
            resolved = resolve_sphinx_path(file_path, match.group(1) or match.group(2))
            if not resolved.endswith('.rst') and '.' not in os.path.basename(resolved): resolved += '.rst'
            deps.add(resolved)
        for match in RE_REF.finditer(content):
            if (lbl := (match.group(1) or match.group(2)).strip()) in label_to_file: deps.add(label_to_file[lbl])
        file_dependencies[file_path] = deps

    cloud_required, onprem_required = set(), set()
    propagate_tags([p for p, t in file_tags.items() if 'Cloud' in t], cloud_required, file_dependencies)
    propagate_tags([p for p, t in file_tags.items() if 'OnPrem' in t], onprem_required, file_dependencies)

    untagged = [f for f in all_files if f.lower() not in ESSENTIAL_BUILD_FILES and f not in cloud_required and f not in onprem_required]
            
    return cloud_required, onprem_required, untagged, file_dependencies, grid_table_files, file_roles

# --- 4. PHASE 2: TRANSLATOR ENGINE ---
def convert_rst_to_md(content, mode, current_rel_path, repo_dir, target_base_dir):
    content = content.replace('|v|', '✅').replace('|x|', '❌')
    content = re.sub(r'^\s*\.\.\s+tabularcolumns::.*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'\.\.\s+only::\s*latex\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', '', content)
    content = re.sub(r'^\s*\.\.\s+rst-class::.*$', '', content, flags=re.MULTILINE)

    def unwrap_block(m): return "\n" + re.sub(r'^[ \t]+', '', m.group(1), flags=re.MULTILINE) + "\n"
    content = re.sub(r'\.\.\s+(?:only::\s*html|container::)\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', unwrap_block, content)

    def handle_meta(m):
        yaml_lines = ["---"]
        for line in m.group(1).split('\n'):
            line = line.strip()
            if line.startswith(':'):
                parts = line.split(':', 2)
                if len(parts) >= 3:
                    yaml_lines.append(f'{parts[1].strip()}: "{parts[2].strip()}"' if ':' in parts[2] else f"{parts[1].strip()}: {parts[2].strip()}")
        return "\n".join(yaml_lines) + "\n---\n\n"
    content = re.sub(r'^\s*\.\.\s+meta::\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_meta, content, flags=re.MULTILINE)

    content = re.sub(r'^([^\n]+)\n[=]{3,}$', r'# \1', content, flags=re.MULTILINE)
    content = re.sub(r'^([^\n]+)\n[-]{3,}$', r'## \1', content, flags=re.MULTILINE)
    content = re.sub(r'^([^\n]+)\n[~]{3,}$', r'### \1', content, flags=re.MULTILINE)
    content = re.sub(r'^([^\n]+)\n[\^]{3,}$', r'#### \1', content, flags=re.MULTILINE)

    content = re.sub(r':doc:`(?:[^<`]*<([^>]+)>|([^`]+))`', lambda m: f"[{m.group(1) or m.group(2)}]({(m.group(1) or m.group(2)).replace('.rst', '')}.md)", content)
    content = re.sub(r':ref:`(?:[^<`]*<([^>]+)>|([^`]+))`', lambda m: f"[{m.group(1) or m.group(2)}](#{(m.group(1) or m.group(2)).lower().replace(' ', '-')})", content)

    def handle_code_block(m):
        code = re.sub(r'^[ \t]+', '', m.group(2), flags=re.MULTILINE)
        return f"```{m.group(1)}\n{code}\n```"
    content = re.sub(r'\.\.\s+code-block::\s*(\w*)\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_code_block, content)

    def handle_raw_html(m):
        html = re.sub(r'^[ \t]+', '', m.group(1), flags=re.MULTILINE)
        return f"\n{html}\n"
    content = re.sub(r'\.\.\s+raw::\s*html\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_raw_html, content)

    def handle_include(m):
        include_path = m.group(1).strip()
        resolved_original = resolve_sphinx_path(current_rel_path, include_path)
        
        if mode == 'flat' or not os.path.exists(os.path.join(target_base_dir, resolved_original)):
            try:
                with open(os.path.join(repo_dir, resolved_original), 'r', encoding='utf-8') as f:
                    return "\n\n" + convert_rst_to_md(f.read(), mode, resolved_original, repo_dir, target_base_dir) + "\n\n"
            except Exception: return ""
                
        if mode == 'mintlify': return f'<Snippet file="{include_path.lstrip("/").replace(".rst", ".mdx")}" />'
        elif mode == 'gitbook': return f'{{% include "{include_path.replace(".rst", ".md")}" %}}'
            
    content = re.sub(r'^\s*\.\.\s+include::\s+(.+)$', handle_include, content, flags=re.MULTILINE)

    def handle_admonition(m):
        adm_type, text = m.group(1).title(), m.group(2)
        text = re.sub(r'^[ \t]*:[a-zA-Z_-]+:.*$\n', '', text, flags=re.MULTILINE)
        unindented = re.sub(r'^[ \t]+', '', text, flags=re.MULTILINE).strip()
        if mode == 'mintlify': return f"<{adm_type}>\n{unindented}\n</{adm_type}>"
        return f"> **{adm_type}**\n> {unindented.replace(chr(10), chr(10) + '> ')}"
    content = re.sub(r'\.\.\s+(note|warning|tip|important|caution|info)::\s*\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_admonition, content)

    def handle_mermaid(m):
        mmd_path = resolve_sphinx_path(current_rel_path, m.group(1).strip())
        abs_path = os.path.join(repo_dir, mmd_path)
        if os.path.exists(abs_path):
            with open(abs_path, 'r', encoding='utf-8') as f:
                return f"```mermaid\n{f.read().strip()}\n```"
        return ""
    content = re.sub(r'^\s*\.\.\s+mermaid::\s+([^\s]+\.mmd)', handle_mermaid, content, flags=re.MULTILINE)
    
    content = re.sub(r'^\s*\.\.\s+video::\s+([^\s]+)', lambda m: f'<video controls width="100%"><source src="{m.group(1).strip().lstrip("/")}" type="video/mp4"></video>', content, flags=re.MULTILINE)
    
    def handle_collapse(m):
        body = re.sub(r'^[ \t]+', '', m.group(2), flags=re.MULTILINE).strip()
        return f"<details>\n<summary>{m.group(1).strip()}</summary>\n\n{body}\n</details>"
    content = re.sub(r'\.\.\s+collapse::\s*(.*?)\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_collapse, content)
    
    content = re.sub(r'^\s*\.\.\s+tabs::\s*\n', '', content, flags=re.MULTILINE)
    
    def handle_tab(m):
        body = re.sub(r'^[ \t]+', '', m.group(2), flags=re.MULTILINE).strip()
        if mode == 'gitbook':
            return f"{{% tab title=\"{m.group(1).strip()}\" %}}\n{body}\n{{% endtab %}}"
        return f"### {m.group(1).strip()}\n\n{body}\n"
    content = re.sub(r'\.\.\s+tab::\s*(.*?)\n+((?:(?:[ \t]+)[^\n]*\n?)+)', handle_tab, content)

    content = re.sub(r'``([^`]+)``', r'`\1`', content)
    
    return content

# --- 5. FILE GENERATION & ZIP ---
def generate_segregated_environment(repo_dir, cloud_required, onprem_required, output_mode):
    staging_dir = os.path.join(tempfile.gettempdir(), f"segregated_docs_{os.urandom(4).hex()}")
    cloud_dir, onprem_dir = os.path.join(staging_dir, "Alation Cloud Service"), os.path.join(staging_dir, "CustomerManaged")
    stats = {"cloud": 0, "onprem": 0}

    def safe_copy(src_rel_path, target_base_dir):
        src_abs = os.path.join(repo_dir, src_rel_path)
        if os.path.exists(src_abs):
            target_abs = os.path.join(target_base_dir, src_rel_path)
            os.makedirs(os.path.dirname(target_abs), exist_ok=True)
            shutil.copy2(src_abs, target_abs)

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
        for file in files:
            rel_path = os.path.relpath(os.path.join(root, file), repo_dir).replace("\\", "/")
            if file.lower() in ESSENTIAL_BUILD_FILES and output_mode == 'rest':
                safe_copy(rel_path, cloud_dir); safe_copy(rel_path, onprem_dir); continue
            if rel_path in cloud_required: safe_copy(rel_path, cloud_dir); stats["cloud"] += 1
            if rel_path in onprem_required: safe_copy(rel_path, onprem_dir); stats["onprem"] += 1

    if output_mode != 'rest':
        for target_env in [cloud_dir, onprem_dir]:
            if not os.path.exists(target_env): continue
            for root, _, files in os.walk(target_env):
                for file in files:
                    if file.endswith('.rst'):
                        file_path = os.path.join(root, file)
                        rel_repo_path = os.path.relpath(file_path, target_env).replace("\\", "/")
                        with open(file_path, 'r', encoding='utf-8') as f: raw_content = f.read()
                        translated_content = convert_rst_to_md(raw_content, output_mode, rel_repo_path, repo_dir, target_env)
                        with open(file_path[:-4] + ('.mdx' if output_mode == 'mintlify' else '.md'), 'w', encoding='utf-8') as f: f.write(translated_content)
                        os.remove(file_path)
                
                # Flush memory periodically during massive directory walks
                gc.collect()

    zip_base_path = os.path.join(tempfile.gettempdir(), "Alation_Final_Docs")
    zip_filepath = shutil.make_archive(zip_base_path, 'zip', staging_dir)
    shutil.rmtree(staging_dir, ignore_errors=True)
    return zip_filepath, stats

# --- 6. UI WORKFLOW ---
def main():
    with st.sidebar:
        st.header("🔑 Credentials Setup")
        github_pat = st.text_input("GitHub PAT", type="password")
        gemini_key = st.text_input("Gemini API Key (For AI Guides)", type="password")
        
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
                if not os.path.exists(os.path.join(REPO_DIR, ".git")): git.Repo.clone_from(auth_url, REPO_DIR)
                else: git.Repo(REPO_DIR).remotes.origin.pull()
                st.session_state['repo_ready'] = True
                st.success("Repository pulled and is up to date!")
            except Exception as e: st.error(f"Failed to fetch repository: {e}")

    if st.session_state.get('repo_ready', False) or os.path.exists(os.path.join(REPO_DIR, ".git")):
        st.divider()
        st.write("### 2. Analyze Dependency Graph")
        
        if st.button("🔍 Scan for Tags & Dependencies"):
            with st.spinner("Mapping dependency graph and user roles..."):
                c_req, o_req, untagged, deps, grid_tables, roles = analyze_dependencies(REPO_DIR)
                st.session_state.update({'cloud_req': c_req, 'onprem_req': o_req, 'deps': deps, 'grid_tables': grid_tables, 'roles': roles})
                st.session_state['untagged_df'] = pd.DataFrame({"File Path": sorted(untagged), "Action": ["Ignore"] * len(untagged)})
                gc.collect() # Free analysis memory
                st.success(f"Found {len(c_req)} Cloud files, {len(o_req)} On-Prem files, and {len(untagged)} Untagged files.")

        if 'untagged_df' in st.session_state:
            st.divider()
            st.write("### 3. Review Untagged / Orphaned Files")
            # Replaced deprecated use_container_width with width="stretch"
            edited_df = st.data_editor(
                st.session_state['untagged_df'],
                column_config={"Action": st.column_config.SelectboxColumn("Action", options=["Ignore", "Alation Cloud Service", "CustomerManaged", "Both"], required=True), "File Path": st.column_config.TextColumn(disabled=True)},
                width="stretch", hide_index=True
            )
            st.download_button("📄 Download Untagged Report (CSV)", data=st.session_state['untagged_df'].to_csv(index=False).encode('utf-8'), file_name="untagged_report.csv", mime="text/csv")

            st.divider()
            st.write("### 4. Standard Format Selection & Build")
            output_format = st.radio("Select Output Architecture:", options=["Sphinx reST (Original)", "Mintlify (MDX)", "GitBook (MD)", "Flat Markdown (MD)"])

            if output_format != "Sphinx reST (Original)" and st.session_state.get('grid_tables'):
                st.warning(f"⚠️ **Warning:** Found {len(st.session_state['grid_tables'])} files containing Sphinx Grid Tables. Standard Markdown does not support grid tables.")

            if st.button("🚀 Apply Manual Tags & Generate Standard ZIP", type="primary"):
                with st.spinner("Applying rules, generating output, and zipping files..."):
                    final_cloud, final_onprem, deps = set(st.session_state['cloud_req']), set(st.session_state['onprem_req']), st.session_state['deps']
                    m_cloud, m_onprem = [r['File Path'] for i, r in edited_df.iterrows() if r['Action'] in ["Alation Cloud Service", "Both"]], [r['File Path'] for i, r in edited_df.iterrows() if r['Action'] in ["CustomerManaged", "Both"]]
                    propagate_tags(m_cloud, final_cloud, deps); propagate_tags(m_onprem, final_onprem, deps)
                    
                    mode_map = {"Sphinx reST (Original)": "rest", "Mintlify (MDX)": "mintlify", "GitBook (MD)": "gitbook", "Flat Markdown (MD)": "flat"}
                    zip_path, stats = generate_segregated_environment(REPO_DIR, final_cloud, final_onprem, mode_map[output_format])
                    
                    st.success("Successfully generated environment ZIP!")
                    with open(zip_path, "rb") as fp:
                        st.download_button("📦 Download Final Output (ZIP)", data=fp, file_name="Alation_Docs_Output.zip", mime="application/zip", type="primary", width="stretch")

            st.divider()
            st.write("### 5. AI Guide Generation")
            st.info("Uses Gemini 2.5 Pro to sequentially synthesize your flat, categorized documentation into structured Admin and User guides. Optimized to prevent Streamlit Cloud timeouts.")
            
            if gemini_key:
                if st.button("🤖 Synthesize AI Guides (Admin/User)", type="primary"):
                    with st.status("Starting AI Generation Engine...", expanded=True) as status:
                        try:
                            genai.configure(api_key=gemini_key)
                            model = genai.GenerativeModel('gemini-2.5-pro')
                            
                            final_cloud, final_onprem = set(st.session_state['cloud_req']), set(st.session_state['onprem_req'])
                            roles = st.session_state['roles']
                            
                            def get_flat_content(files_set):
                                content_str = ""
                                for file in files_set:
                                    abs_p = os.path.join(REPO_DIR, file)
                                    if os.path.exists(abs_p) and file.endswith('.rst'):
                                        with open(abs_p, 'r', encoding='utf-8') as f:
                                            content_str += f"\n\n--- Source: {file} ---\n" + convert_rst_to_md(f.read(), 'flat', file, REPO_DIR, REPO_DIR)
                                return content_str

                            status.update(label="Building Global System Context...")
                            system_context = "System Instructions:\n"
                            for p in [GLOSSARY_PATH, ROLES_PATH]:
                                if os.path.exists(os.path.join(REPO_DIR, p)):
                                    with open(os.path.join(REPO_DIR, p), 'r', encoding='utf-8') as f:
                                        system_context += f"\n{convert_rst_to_md(f.read(), 'flat', p, REPO_DIR, REPO_DIR)}"
                            
                            bucket_definitions = {
                                "Cloud_Admin_Guide": {f for f in final_cloud if roles.get(f) == 'Admin'},
                                "Cloud_User_Guide": {f for f in final_cloud if roles.get(f) == 'User'},
                                "OnPrem_Admin_Guide": {f for f in final_onprem if roles.get(f) == 'Admin'},
                                "OnPrem_User_Guide": {f for f in final_onprem if roles.get(f) == 'User'}
                            }

                            ai_staging = os.path.join(tempfile.gettempdir(), f"ai_guides_{os.urandom(4).hex()}")
                            os.makedirs(ai_staging, exist_ok=True)
                            
                            for bucket_name, files_set in bucket_definitions.items():
                                if not files_set: continue
                                
                                status.update(label=f"Flattening content for {bucket_name}...")
                                flat_content = get_flat_content(files_set)
                                
                                if not flat_content.strip(): continue
                                
                                status.update(label=f"Calling Gemini 2.5 Pro for {bucket_name}... (This takes 1-3 minutes)")
                                prompt = f"{system_context}\n\nTask: You are an expert Alation Technical Writer. Using ONLY the raw documentation provided below, synthesize a comprehensive, cohesive, logically flowing Markdown guide specifically for '{bucket_name}'.\n- Create a logical Table of Contents.\n- Group similar topics.\n- Preserve all technical accuracy, code blocks, and configuration steps. Do not hallucinate features.\n\nRaw Content:\n{flat_content[:3000000]}"
                                
                                try:
                                    response = model.generate_content(prompt)
                                    with open(os.path.join(ai_staging, f"{bucket_name}.md"), 'w', encoding='utf-8') as f: f.write(response.text)
                                except Exception as e:
                                    st.error(f"Error generating {bucket_name}: {e}")
                                
                                # Instantly kill the massive string from RAM after generating to avoid OOM
                                del flat_content 
                                gc.collect()
                                
                            ai_zip = shutil.make_archive(os.path.join(tempfile.gettempdir(), "AI_Generated_Guides"), 'zip', ai_staging)
                            status.update(label="✅ All AI Guides Generated Successfully!", state="complete", expanded=False)
                            
                            with open(ai_zip, "rb") as fp:
                                st.download_button("📥 Download AI Generated Guides (ZIP)", data=fp, file_name="AI_Generated_Guides.zip", mime="application/zip", type="primary", width="stretch")

                        except Exception as e:
                            status.update(label="❌ AI Generation Failed", state="error", expanded=True)
                            st.error(f"Critical Failure: {e}")

            else:
                st.warning("⚠️ Enter your Gemini API Key in the sidebar to unlock AI Guide Generation.")

if __name__ == "__main__":
    main()
