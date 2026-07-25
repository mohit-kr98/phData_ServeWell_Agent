import os
import streamlit as st
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

API_URL = os.environ.get("API_URL", "http://localhost:8000")
INGESTION_URL = os.environ.get("INGESTION_URL", "http://localhost:8001")
QUERY_URL = os.environ.get("QUERY_URL", "http://localhost:8002")
ADMIN_URL = os.environ.get("ADMIN_URL", "http://localhost:8003")

# We leave triage/resolve to point to 8000 for now if they are not containerized, 
# but if they are, we could override them too. Let's just hardcode 8000 as before unless configured
TICKET_URL = os.environ.get("TICKET_URL", "http://localhost:8000")

def to_container_path(host_path: str) -> str:
    """Converts a host path to the corresponding container path."""
    from pathlib import Path
    project_dir = str(Path(__file__).parent.absolute())
    if host_path.startswith(project_dir):
        return host_path.replace(project_dir, "/app", 1)
    return host_path

if 'ticket_directory' not in st.session_state:
    st.session_state.ticket_directory = "/app/tickets/train"

st.set_page_config(page_title="ServeWell IT Agent POC", layout="wide")

st.title("ServeWell Agentic IT Support")
st.markdown("A proof-of-concept L1 support system built on the phData Intelligence Platform.")

def select_folder_via_tk():
    """Open system folder picker using OS-specific commands without blocking Streamlit.
    On macOS, uses AppleScript via `osascript`. Falls back to Tkinter on other platforms.
    Returns the selected folder path as a string, or ``None`` if cancelled or on error.
    """
    import sys, subprocess
    try:
        if sys.platform == "darwin":
            # AppleScript chooser returns POSIX path with a trailing newline
            result = subprocess.run(
                ["osascript", "-e", "POSIX path of (choose folder)"],
                capture_output=True,
                text=True,
                check=False,
            )
            path = result.stdout.strip()
            return path if path else None
        else:
            # Fallback to Tkinter for Windows / Linux
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes('-topmost', 1)
            folder_selected = filedialog.askdirectory(master=root)
            root.destroy()
            return folder_selected if folder_selected else None
    except Exception:
        return None

def load_tickets_from_dir(directory_path):
    """Recursively loads all JSON tickets from the given directory and its subdirectories into memory dicts."""
    target_dir = Path(directory_path)
    loaded_data = {}
    loaded_raw = {}
    errors = []

    if target_dir.exists() and target_dir.is_dir():
        # Use rglob to find JSON files in all nested subdirectories
        json_files = sorted(list(target_dir.rglob("*.json")))
        for fpath in json_files:
            ticket_id = fpath.stem
            try:
                content = None
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        content = f.read()
                except OSError as e:
                    # Handle Docker volume deadlock or similar read errors
                    if getattr(e, 'errno', None) == 35:
                        import subprocess
                        content = subprocess.check_output(["cat", str(fpath)]).decode("utf-8")
                    else:
                        raise e

                if content:
                    parsed = json.loads(content)
                    loaded_data[ticket_id] = parsed
                    loaded_raw[ticket_id] = content
            except Exception as ex:
                errors.append(f"{fpath.name}: {ex}")
                
    return loaded_data, loaded_raw, errors

if hasattr(st, "dialog"):
    @st.dialog("📂 Directory Browser", width="large")
    def render_directory_picker_dialog(target_key):
        st.markdown("Navigate to select a directory, or open your system file picker.")
        
        current_val = st.session_state.get(target_key, "./")
        if "picker_current_dir" not in st.session_state:
            try:
                p = Path(current_val).resolve()
                if p.exists() and p.is_dir():
                    st.session_state.picker_current_dir = str(p)
                else:
                    st.session_state.picker_current_dir = str(Path.cwd())
            except Exception:
                st.session_state.picker_current_dir = str(Path.cwd())

        curr_path = Path(st.session_state.picker_current_dir)

        # Quick Navigation & OS Dialog Action Bar
        col_tk, col_up, col_home = st.columns([2, 1, 1])
        with col_tk:
            if st.button("🖥️ System Folder Picker", help="Open native macOS Finder / Windows File Explorer", use_container_width=True):
                folder = select_folder_via_tk()
                if folder:
                    st.session_state[target_key] = folder
                    if "show_picker_for" in st.session_state:
                        del st.session_state["show_picker_for"]
                    if "picker_current_dir" in st.session_state:
                        del st.session_state["picker_current_dir"]
                    st.rerun()
        with col_up:
            if st.button("⬆️ Up Level", disabled=(curr_path.parent == curr_path), use_container_width=True):
                st.session_state.picker_current_dir = str(curr_path.parent)
                st.rerun()
        with col_home:
            if st.button("🏠 Project Root", use_container_width=True):
                st.session_state.picker_current_dir = str(Path.cwd())
                st.rerun()

        st.caption(f"📁 **Current Location:** `{curr_path.absolute()}`")

        # Subdirectories listing
        subdirs = []
        file_count = 0
        md_count = 0
        json_count = 0
        try:
            if curr_path.exists() and curr_path.is_dir():
                for item in curr_path.iterdir():
                    if item.name.startswith("."):
                        continue
                    if item.is_dir():
                        subdirs.append(item)
                    elif item.is_file():
                        file_count += 1
                        if item.suffix.lower() == ".md":
                            md_count += 1
                        elif item.suffix.lower() == ".json":
                            json_count += 1
            subdirs.sort(key=lambda x: x.name.lower())
        except Exception as e:
            st.error(f"Error accessing directory: {e}")

        st.markdown(f"**Subfolders in `{curr_path.name or '/'}`** ({len(subdirs)} found):")
        
        if subdirs:
            dir_names = [f"📁 {d.name}" for d in subdirs]
            col_sel, col_open = st.columns([3, 1])
            with col_sel:
                selected_dir_name = st.selectbox(
                    "Subfolders", 
                    dir_names, 
                    label_visibility="collapsed",
                    key="picker_subfolder_select"
                )
            with col_open:
                if st.button("Open ➡️", use_container_width=True):
                    idx = dir_names.index(selected_dir_name)
                    st.session_state.picker_current_dir = str(subdirs[idx])
                    st.rerun()
        else:
            st.info("No subdirectories here.")

        st.caption(f"📊 Folder stats: {file_count} files ({md_count} `.md` files, {json_count} `.json` files)")

        st.divider()

        col_select, col_cancel = st.columns([1, 1])
        with col_select:
            if st.button("✅ Select This Directory", type="primary", use_container_width=True):
                st.session_state[target_key] = str(curr_path.absolute())
                if "show_picker_for" in st.session_state:
                    del st.session_state["show_picker_for"]
                if "picker_current_dir" in st.session_state:
                    del st.session_state["picker_current_dir"]
                st.rerun()
        with col_cancel:
            if st.button("❌ Cancel", use_container_width=True):
                if "show_picker_for" in st.session_state:
                    del st.session_state["show_picker_for"]
                if "picker_current_dir" in st.session_state:
                    del st.session_state["picker_current_dir"]
                st.rerun()

tab_tickets, tab_kb, tab_faiss = st.tabs(["Ticket Processing", "Knowledge Base Management", "PGVector Database"])

with tab_kb:
    st.header("Knowledge Base Ingestion")
    
    if 'kb_directory' not in st.session_state:
        st.session_state.kb_directory = "./kb"

    st.subheader("Select Directory for Ingestion")
    if "ingestion_success" in st.session_state:
        st.success(st.session_state.ingestion_success)
        del st.session_state.ingestion_success
        
    col_input, col_browse = st.columns([4, 1])
    with col_input:
        st.text_input(
            "Enter the path to the knowledge base directory:",
            key="kb_directory"
        )
    with col_browse:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        if st.button("📁 Browse...", key="browse_kb_btn", use_container_width=True):
            render_directory_picker_dialog("kb_directory")
    target_dir = st.session_state.kb_directory
    target_path = Path(target_dir)
    if target_path.exists() and target_path.is_dir():
        md_count = len(list(target_path.rglob("*.md")))
        st.success(f"Found {md_count} markdown (.md) files in `{target_dir}` and its subdirectories.")
    else:
        st.error("The selected directory does not exist.")

    if st.button("Preview Ingestion", help="Parses the target directory and shows a preview without generating embeddings."):
        with st.status(f"Parsing '{target_dir}'...", expanded=True) as preview_status:
            target_path = Path(target_dir)
            md_files = list(target_path.rglob("*.md"))
            
            st.write(f"🔍 Discovered {len(md_files)} Markdown files.")

            st.write("⚙️ Connecting to ingestion backend to chunk documents...")
            try:
                res = requests.post(f"{INGESTION_URL}/preview", json={"directory": to_container_path(target_dir)})
                res.raise_for_status()
                st.session_state.preview_data = res.json()
                preview_status.update(label="Parsing Complete!", state="complete")
            except Exception as e:
                preview_status.update(label="Failed to parse directory", state="error")
                st.error(f"Error: {e}")

    if "preview_data" in st.session_state:
        st.divider()
        st.subheader("📊 Preview Results")
        p_data = st.session_state.preview_data
        
        # Display Metrics
        met1, met2, met3 = st.columns(3)
        met1.metric(label="Total Chunks", value=p_data.get('total_chunks', 0))
        met2.metric(label="Categories", value=len(p_data.get('categories', [])))
        met3.metric(label="Status", value="Ready to Embed")
        
        st.write(f"**Included Categories**: `{', '.join(p_data.get('categories', []))}`")
        st.markdown("<br>", unsafe_allow_html=True)
        
        import pandas as pd
        def clean_str(val):
            if not val:
                return ""
            return str(val).replace("\r\n", " ↵ ").replace("\r", " ↵ ").replace("\n", " ↵ ").replace("\t", "    ")

        try:
            with st.expander("📁 Browse Chunks by Category", expanded=True):
                categories = list(p_data.get("samples", {}).keys())
                if categories:
                    col1, col2 = st.columns([2, 1])
                    with col1:
                        selected_cat = st.selectbox("Select category to preview:", categories, key="selected_cat_preview")
                    
                    samples = p_data.get("samples", {}).get(selected_cat, [])
                    chunks_per_page = 5
                    total_pages = (len(samples) - 1) // chunks_per_page + 1
                    
                    with col2:
                        page = st.number_input("Page:", min_value=1, max_value=max(1, total_pages), step=1, value=1, key="preview_page_number")
                    
                    start_idx = (page - 1) * chunks_per_page
                    end_idx = start_idx + chunks_per_page
                    page_samples = samples[start_idx:end_idx]
                    
                    st.write(f"Showing chunks {start_idx + 1} to {min(end_idx, len(samples))} of **{len(samples)}** in **{selected_cat}**:")
                    
                    if page_samples:
                        df = pd.DataFrame([{
                            "File": clean_str(s["metadata"].get("file_name", "")),
                            "Section": clean_str(s["metadata"].get("section_title", "No Title") or "No Title"),
                            "Type": clean_str(s["metadata"].get("section_type", "")),
                            "Content": clean_str(s["content"])[:300] + ("..." if len(str(s["content"])) > 300 else "")
                        } for s in page_samples])
                        st.table(df)
                    else:
                        st.write("No chunks on this page.")
                else:
                    st.write("No categories found.")
                        
                # Detailed chunk inspector
                all_flat_samples = []
                for category, samples in p_data.get("samples", {}).items():
                    for s in samples:
                        all_flat_samples.append(s)
                
                if all_flat_samples:
                    st.markdown("---")
                    st.subheader("🔍 Full Chunk Inspector")
                    st.info("Select any chunk from the dropdown below to view its complete, untruncated content and metadata.")
                    
                    search_query = st.text_input("Filter chunks by keyword (searches File, Section, and Content):", "", key="search_query_input").strip().lower()
                    
                    filtered_indices = []
                    for idx, s in enumerate(all_flat_samples):
                        file_name = s['metadata'].get('file_name', '').lower()
                        section_title = s['metadata'].get('section_title', 'No Title').lower()
                        content = s.get('content', '').lower()
                        
                        if not search_query or search_query in file_name or search_query in section_title or search_query in content:
                            filtered_indices.append(idx)
                    
                    if filtered_indices:
                        chunk_options = [
                            f"[{all_flat_samples[idx]['metadata'].get('file_name', '')}] {all_flat_samples[idx]['metadata'].get('section_title', 'No Title')} (Chunk {idx})"
                            for idx in filtered_indices
                        ]
                        
                        selected_filtered_idx = st.selectbox(
                            "Choose a chunk to inspect:",
                            range(len(filtered_indices)),
                            format_func=lambda x: chunk_options[x],
                            key="selected_chunk_inspect"
                        )
                        
                        selected_chunk = all_flat_samples[filtered_indices[selected_filtered_idx]]
                        col_content, col_meta = st.columns([2, 1])
                        with col_content:
                            st.markdown("**📝 Raw Content**")
                            st.text_area("Content", value=selected_chunk["content"], height=300, label_visibility="collapsed")
                        with col_meta:
                            st.markdown("**🏷️ Metadata**")
                            st.json(selected_chunk["metadata"])
                    else:
                        st.warning("No chunks matched your search query.")
        except Exception as e:
            import traceback
            st.error(f"UI Rendering Error: {e}")
            st.code(traceback.format_exc())

        if st.button("✅ Confirm & Generate Embeddings", type="primary"):
            with st.status(f"Training Vector DB from '{target_dir}'...", expanded=True) as train_status:
                st.write("Generating embeddings...")
                try:
                    res = requests.post(f"{INGESTION_URL}/ingest", json={"directory": to_container_path(target_dir)})
                    res.raise_for_status()
                    data = res.json()
                    st.write(data.get("message", "Ingestion successful."))
                    train_status.update(label="Training Complete!", state="complete")
                    
                    # Set success message to persist after rerun
                    st.session_state.ingestion_success = f"✅ Success! {data.get('message', 'Vector database embeddings generated and indexed successfully.')}"
                    
                    del st.session_state["preview_data"]
                    st.rerun()
                except Exception as e:
                    train_status.update(label="Failed to train vector db", state="error")
                    st.error(f"Error: {e}")
                    
        st.write("---")
        st.subheader("Index Maintenance")
        if st.button("🔄 Rebuild Main Index", help="Merge all Delta ingestion updates into the Main HNSW graph."):
            with st.status("Rebuilding Main Index...", expanded=True) as rebuild_status:
                try:
                    res = requests.post(f"{INGESTION_URL}/rebuild_main_index")
                    res.raise_for_status()
                    data = res.json()
                    st.write(data.get("message", "Rebuild successful."))
                    rebuild_status.update(label="Rebuild Complete!", state="complete")
                except Exception as e:
                    rebuild_status.update(label="Failed to rebuild index", state="error")
                    st.error(f"Error: {e}")

with tab_tickets:
    st.header("Ticket Processing")
    
    if 'ticket_directory' not in st.session_state:
        st.session_state.ticket_directory = "./tickets/train"

    st.subheader("1. Ticket Source")
    col_input, col_browse, col_load, col_ingest = st.columns([3, 1, 1, 1])
    with col_input:
        st.text_input(
            "Enter the path to the tickets directory:", 
            key="ticket_directory"
        )
    with col_browse:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        if st.button("📁 Browse...", key="browse_ticket_btn", use_container_width=True):
            render_directory_picker_dialog("ticket_directory")
    with col_load:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        if st.button("📥 Load Tickets", key="load_tickets_btn", type="primary", use_container_width=True):
            with st.spinner(f"Loading tickets from `{st.session_state.ticket_directory}` into memory..."):
                t_data, t_raw, errs = load_tickets_from_dir(st.session_state.ticket_directory)
                st.session_state.loaded_tickets = t_data
                st.session_state.loaded_tickets_raw = t_raw
                st.session_state.loaded_tickets_dir = st.session_state.ticket_directory
                if errs:
                    st.warning(f"Loaded {len(t_data)} tickets into memory ({len(errs)} failed).")
                else:
                    st.success(f"Successfully loaded {len(t_data)} tickets into memory!")
    with col_ingest:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        if st.button("✅ Ingest Tickets", key="ingest_tickets_btn", type="secondary", use_container_width=True, help="Ingest JSON tickets into the Vector Database"):
            with st.status(f"Ingesting tickets from '{st.session_state.ticket_directory}'...", expanded=True) as train_status:
                try:
                    res = requests.post(f"{INGESTION_URL}/ingest", json={"directory": to_container_path(st.session_state.ticket_directory)})
                    res.raise_for_status()
                    data = res.json()
                    st.write(data.get("message", "Ingestion successful."))
                    train_status.update(label="Ingestion Complete!", state="complete")
                except Exception as e:
                    train_status.update(label="Failed to ingest tickets", state="error")
                    st.error(f"Error: {e}")
    
    TRAIN_TICKETS_DIR = Path(st.session_state.ticket_directory)
    
    if (st.session_state.get('loaded_tickets') is not None and 
        st.session_state.get('loaded_tickets_dir') == st.session_state.ticket_directory):
        ticket_ids = sorted(list(st.session_state.loaded_tickets.keys()))
        st.info(f"⚡ **Memory Cache Active:** {len(ticket_ids)} tickets loaded in memory from `{st.session_state.loaded_tickets_dir}`")
    else:
        def get_available_ticket_ids(directory_path):
            if directory_path.exists() and directory_path.is_dir():
                return sorted([f.stem for f in directory_path.rglob("*.json")])
            return []
        ticket_ids = get_available_ticket_ids(TRAIN_TICKETS_DIR)
    
    st.divider()
    
    ticket_step = st.radio(
        "2. Select Action:",
        ["Inspect Individual Ticket", "Process Individual Ticket", "Batch Process Tickets", "View Processed History"],
        horizontal=True
    )
    
    st.divider()

    if ticket_step == "Inspect Individual Ticket":
        st.subheader("Inspect Individual Tickets")
        if not ticket_ids:
            st.warning(f"No JSON tickets found in `{TRAIN_TICKETS_DIR}`")
        else:
            st.info(f"Found {len(ticket_ids)} tickets in `{TRAIN_TICKETS_DIR}`")
        
        inspect_ticket_id = st.selectbox(
            "Select a specific ticket to view its contents:",
            options=ticket_ids,
            key="inspect_ticket_selectbox"
        )
        
        if inspect_ticket_id:
            try:
                inspect_data = None
                if (st.session_state.get("loaded_tickets") and 
                    inspect_ticket_id in st.session_state.loaded_tickets):
                    inspect_data = st.session_state.loaded_tickets[inspect_ticket_id]
                else:
                    found = list(TRAIN_TICKETS_DIR.rglob(f"{inspect_ticket_id}.json"))
                    if not found:
                        raise FileNotFoundError(f"Ticket {inspect_ticket_id}.json not found in {TRAIN_TICKETS_DIR}")
                    ticket_path = found[0]
                    try:
                        with open(ticket_path, 'r') as f:
                            inspect_content = f.read()
                            inspect_data = json.loads(inspect_content)
                    except OSError as e:
                        if e.errno == 35:
                            import subprocess
                            try:
                                inspect_content = subprocess.check_output(["cat", str(ticket_path)]).decode("utf-8")
                                inspect_data = json.loads(inspect_content)
                            except subprocess.CalledProcessError:
                                st.error(f"Failed to read ticket {inspect_ticket_id} due to Docker volume deadlock.")
                                st.stop()
                        else:
                            raise e
                
                col1, col2 = st.columns([1, 1])
                with col1:
                    st.markdown(f"**ID:** {inspect_data.get('ticket_id')}")
                    st.markdown(f"**Priority:** {inspect_data.get('priority')} | **Category:** {inspect_data.get('category')}")
                with col2:
                    st.markdown(f"**Subject:** {inspect_data.get('subject')}")
                    st.markdown(f"**Asset ID:** {inspect_data.get('asset_id')}")
                
                with st.expander("View Raw JSON"):
                    st.json(inspect_data)
            except Exception as e:
                st.error(f"Could not load ticket {inspect_ticket_id}: {e}")
                
    elif ticket_step == "Process Individual Ticket":
        st.subheader("Process Individual Ticket")
        if not ticket_ids:
            st.warning(f"No JSON tickets found in `{TRAIN_TICKETS_DIR}`")
        else:
            process_ticket_id = st.selectbox(
                "Select a specific ticket to process:",
                options=ticket_ids,
                key="process_ticket_selectbox"
            )
            
            if process_ticket_id:
                ticket_json = None
                if (st.session_state.get("loaded_tickets_raw") and 
                    process_ticket_id in st.session_state.loaded_tickets_raw):
                    ticket_json = st.session_state.loaded_tickets_raw[process_ticket_id]
                else:
                    found = list(TRAIN_TICKETS_DIR.rglob(f"{process_ticket_id}.json"))
                    if found:
                        ticket_path = found[0]
                        try:
                            with open(ticket_path, 'r') as f:
                                ticket_json = f.read()
                        except OSError as e:
                            if e.errno == 35:
                                import subprocess
                                try:
                                    ticket_json = subprocess.check_output(["cat", str(ticket_path)]).decode("utf-8")
                                except subprocess.CalledProcessError:
                                    st.error("Failed to read ticket due to Docker volume deadlock.")
                                    st.stop()
                            else:
                                raise e
                    else:
                        st.error(f"Ticket {process_ticket_id}.json not found in {TRAIN_TICKETS_DIR}")
                        st.stop()
                
                try:
                    ticket_data = json.loads(ticket_json)
                    st.info(f"**Subject:** {ticket_data.get('subject', 'N/A')}\n\n**Description:** {ticket_data.get('description', 'N/A')}")
                except Exception as e:
                    st.warning(f"Could not parse ticket JSON: {e}")

                state_key = f"ticket_state_{process_ticket_id}"
                if state_key not in st.session_state:
                    st.session_state[state_key] = {
                        "started": False,
                        "chat_history": [],
                        "trace": [],
                        "routing": "",
                        "reasoning": "",
                        "status": "" 
                    }
                
                state = st.session_state[state_key]
                
                if not state["started"]:
                    if st.button("Process Ticket", type="primary", key="process_single"):
                        state["started"] = True
                        try:
                            with st.status(f"Processing Ticket {process_ticket_id}..."):
                                st.write("Triaging ticket...")
                                res = requests.post(f"{TICKET_URL}/triage", json={"ticket_json": ticket_json})
                                res.raise_for_status()
                                triage_result = res.json()
                                state["routing"] = triage_result.get('routing', 'ERROR')
                                state["reasoning"] = triage_result.get('reasoning', 'No reasoning provided.')
                                
                                if state["routing"] == "L1_GUIDED":
                                    st.write("Resolving ticket (L1_GUIDED)...")
                                    res = requests.post(f"{TICKET_URL}/resolve", json={"ticket_json": ticket_json, "chat_history": state["chat_history"]})
                                    res.raise_for_status()
                                    resolve_result = res.json()
                                    final_resp = resolve_result.get("final_response", "Error")
                                    
                                    state["chat_history"].append({"role": "assistant", "content": final_resp})
                                    state["trace"].extend(resolve_result.get("trace", []))
                                    
                                    if "Escalated" in final_resp or "Error" in final_resp:
                                        state["status"] = "escalated"
                                    elif "Please attempt" in final_resp or "let me know" in final_resp or "?" in final_resp:
                                        state["status"] = "awaiting_user"
                                    else:
                                        state["status"] = "resolved"
                                        
                                    st.rerun()
                                else:
                                    state["status"] = "resolved"
                                    st.rerun()
                        except Exception as e:
                            st.error(f"Error processing ticket {process_ticket_id}: {e}")
                else:
                    st.markdown(f"**Routing:** `{state['routing']}`")
                    st.markdown(f"**Reasoning:** {state['reasoning']}")
                    
                    if state["routing"] == "L1_GUIDED":
                        st.markdown("### Conversation")
                        
                        for msg in state["chat_history"]:
                            with st.chat_message(msg["role"]):
                                st.write(msg["content"])
                                
                        if state["status"] == "awaiting_user":
                            col1, col2 = st.columns([3, 1])
                            with col2:
                                if st.button("Close Ticket (Resolved)", use_container_width=True):
                                    state["status"] = "resolved"
                                    try:
                                        history_payload = {
                                            "ticket_id": process_ticket_id,
                                            "subject": ticket_data.get('subject', 'N/A') if 'ticket_data' in locals() else 'N/A',
                                            "routing": state["routing"],
                                            "reasoning": state["reasoning"],
                                            "resolution": "Closed by user."
                                        }
                                        requests.post(f"{TICKET_URL}/save_history", json=history_payload, timeout=2)
                                        st.success("Ticket closed and saved.")
                                    except Exception:
                                        pass
                                    st.rerun()
                                    
                            user_reply = st.chat_input("Reply to the agent...")
                            if user_reply:
                                state["chat_history"].append({"role": "user", "content": user_reply})
                                with st.spinner("Agent is reasoning..."):
                                    try:
                                        res = requests.post(f"{TICKET_URL}/resolve", json={"ticket_json": ticket_json, "chat_history": state["chat_history"]})
                                        res.raise_for_status()
                                        resolve_result = res.json()
                                        final_resp = resolve_result.get("final_response", "Error")
                                        
                                        state["chat_history"].append({"role": "assistant", "content": final_resp})
                                        state["trace"].extend(resolve_result.get("trace", []))
                                        
                                        if "Escalated" in final_resp or "Error" in final_resp:
                                            state["status"] = "escalated"
                                        elif "Please attempt" in final_resp or "let me know" in final_resp or "?" in final_resp:
                                            state["status"] = "awaiting_user"
                                        else:
                                            state["status"] = "resolved"
                                    except Exception as e:
                                        st.error(f"Error: {e}")
                                st.rerun()
                        with st.expander("View Agent Trace"):
                            for step in state["trace"]:
                                if step.get('type') == 'tool_result' and step.get('name') == 'search_knowledge_base':
                                    st.markdown("📄 **Retrieved Chunks:**")
                                    st.info(step.get('result', ''))                

                    if state["status"] == "resolved" or state["status"] == "escalated":
                        st.success(f"Ticket processing finished. Final Status: {state['status'].upper()}")
                        if st.button("Start Over", key="restart"):
                            del st.session_state[state_key]
                            st.rerun()
    elif ticket_step == "Batch Process Tickets":
        st.subheader("Batch Process Tickets")
        if not ticket_ids:
            st.warning(f"No JSON tickets found in `{TRAIN_TICKETS_DIR}`")
        else:
            batch_limit = st.number_input("Batch Limit (0 = all)", min_value=0, max_value=len(ticket_ids), value=10, key="batch_process_limit")
            if st.button("Process Batch", type="primary"):
                st.divider()
                st.subheader("Batch Processing Results")
                
                tickets_to_process = ticket_ids[:batch_limit] if batch_limit > 0 else ticket_ids
                
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                results = []
                
                for i, ticket_id in enumerate(tickets_to_process):
                    status_text.text(f"Processing ticket {i+1} of {len(tickets_to_process)}: {ticket_id}")
                    
                    # Load ticket
                    ticket_data = None
                    ticket_json = None
                    
                    if (st.session_state.get("loaded_tickets") and 
                        st.session_state.get("loaded_tickets_raw") and 
                        ticket_id in st.session_state.loaded_tickets):
                        ticket_data = st.session_state.loaded_tickets[ticket_id]
                        ticket_json = st.session_state.loaded_tickets_raw[ticket_id]
                    else:
                        found = list(TRAIN_TICKETS_DIR.rglob(f"{ticket_id}.json"))
                        if not found:
                            results.append({"ID": ticket_id, "Subject": "Error", "Routing": "ERROR", "Reasoning": "File not found", "Trace": [], "Final Response": "Error"})
                            progress_bar.progress((i + 1) / len(tickets_to_process))
                            continue
                        ticket_path = found[0]
                        try:
                            with open(ticket_path, 'r') as f:
                                content = f.read()
                                ticket_data = json.loads(content)
                                ticket_json = content
                        except OSError as e:
                            if e.errno == 35:  # Resource deadlock avoided (Mac Docker volume bug)
                                import subprocess
                                try:
                                    content = subprocess.check_output(["cat", str(ticket_path)]).decode("utf-8")
                                    ticket_data = json.loads(content)
                                    ticket_json = content
                                except subprocess.CalledProcessError:
                                    results.append({"ID": ticket_id, "Subject": "Error", "Routing": "ERROR", "Reasoning": "Mac Docker Volume Deadlock", "Trace": [], "Final Response": "Error"})
                                    progress_bar.progress((i + 1) / len(tickets_to_process))
                                    continue
                                except Exception as sub_e:
                                    results.append({"ID": ticket_id, "Subject": "Error", "Routing": "ERROR", "Reasoning": str(sub_e), "Trace": [], "Final Response": "Error"})
                                    progress_bar.progress((i + 1) / len(tickets_to_process))
                                    continue
                            else:
                                results.append({"ID": ticket_id, "Subject": "Error", "Routing": "ERROR", "Reasoning": str(e), "Trace": [], "Final Response": "Error"})
                                progress_bar.progress((i + 1) / len(tickets_to_process))
                                continue
                        except Exception as e:
                            results.append({"ID": ticket_id, "Subject": "Error", "Routing": "ERROR", "Reasoning": str(e), "Trace": [], "Final Response": "Error"})
                            progress_bar.progress((i + 1) / len(tickets_to_process))
                            continue
                    
                    # Triage
                    try:
                        res = requests.post(f"{TICKET_URL}/triage", json={"ticket_json": ticket_json})
                        res.raise_for_status()
                        triage_result = res.json()
                        routing = triage_result.get('routing', 'ERROR')
                        reasoning = triage_result.get('reasoning', 'No reasoning provided.')
                    except Exception as e:
                        routing = "ERROR"
                        reasoning = str(e)
                    
                    # Resolution if needed
                    trace = []
                    final_response = "N/A"
                    if routing == "L1_GUIDED":
                        try:
                            res = requests.post(f"{TICKET_URL}/resolve", json={"ticket_json": ticket_json})
                            res.raise_for_status()
                            resolve_result = res.json()
                            final_response = resolve_result.get("final_response", "Error")
                            trace = resolve_result.get("trace", [])
                        except Exception as e:
                            final_response = "Error"
                            trace = [{"type": "reasoning", "text": str(e)}]
                    
                    results.append({
                        "ID": ticket_data.get('ticket_id', ticket_id) if isinstance(ticket_data, dict) else ticket_id,
                        "Subject": ticket_data.get('subject', 'Unknown') if isinstance(ticket_data, dict) else 'Unknown',
                        "Routing": routing,
                        "Reasoning": reasoning,
                        "Final Response": final_response,
                        "Trace": trace
                    })
                    
                    try:
                        history_payload = {
                            "ticket_id": ticket_data.get('ticket_id', ticket_id) if isinstance(ticket_data, dict) else ticket_id,
                            "subject": ticket_data.get('subject', 'Unknown') if isinstance(ticket_data, dict) else 'Unknown',
                            "routing": routing,
                            "reasoning": reasoning,
                            "resolution": final_response
                        }
                        requests.post(f"{TICKET_URL}/save_history", json=history_payload, timeout=2)
                    except Exception:
                        pass
                    
                    progress_bar.progress((i + 1) / len(tickets_to_process))
                
                status_text.text("Batch processing complete!")
                
                # Display results
                import pandas as pd
                df = pd.DataFrame(results)[["ID", "Subject", "Routing", "Reasoning", "Final Response"]]
                st.dataframe(df, use_container_width=True)
                
                st.subheader("Detailed Traces")
                for res in results:
                    with st.expander(f"{res['ID']} - {res['Routing']}"):
                        st.markdown(f"**Subject:** {res['Subject']}")
                        st.markdown(f"**Reasoning:** {res['Reasoning']}")
                        st.markdown(f"**Final Response:** {res['Final Response']}")
                        if res['Trace']:
                            st.write("---")
                            st.write("**Agent Trace:**")
                            for step in res['Trace']:
                                if step.get('type') == 'tool_call':
                                    st.markdown(f"🛠️ **Tool Call:** `{step.get('name')}`")
                                    st.json(step.get('args', {}))
                                elif step.get('type') == 'tool_result':
                                    st.markdown("📄 **Retrieved Chunks / Tool Result:**")
                                    st.info(step.get('result', ''))
                                elif step.get('type') == 'reasoning':
                                    st.markdown(f"🧠 **Agent Thought:** {step.get('text')}")
                                    
    elif ticket_step == "View Processed History":
        st.subheader("View Processed History")
        if st.button("Refresh History", key="refresh_history"):
            pass
            
        try:
            res = requests.get(f"{TICKET_URL}/history", timeout=5)
            res.raise_for_status()
            history_data = res.json().get("history", [])
            if not history_data:
                st.info("No tickets have been processed yet.")
            else:
                import pandas as pd
                df = pd.DataFrame(history_data)
                st.dataframe(df, use_container_width=True)
        except Exception as e:
            st.error(f"Failed to fetch history: {e}")

with tab_faiss:
    st.header("PGVector Database")
    st.write("View and search the current embeddings stored in the PostgreSQL database.")
    
    MAIN_COLLECTION = "main_index"
    DELTA_COLLECTION = "delta_index"
    
    try:
        # Fetch Stats
        try:
            stats_res = requests.get(f"{ADMIN_URL}/pgvector/stats", timeout=5)
            stats_res.raise_for_status()
            total_vectors = stats_res.json().get("total_vectors", 0)
        except Exception:
            total_vectors = 0
            
        st.metric("Total Vectors (Main + Delta)", total_vectors)
        
        st.markdown("---")
        st.subheader("Semantic Search")
        query = st.text_input("Enter a search query to test the vector database:")
        
        if query:
            with st.spinner("Searching and Reranking..."):
                try:
                    search_res = requests.post(f"{ADMIN_URL}/pgvector/search", json={"query": query, "initial_k": 15})
                    search_res.raise_for_status()
                    data = search_res.json()
                    unique_fetched = data.get("unique_fetched", 0)
                    results = data.get("results", [])
                    
                    st.write(f"Fetched top chunks from Main & Delta (Unique: {unique_fetched}). Applied Cross-Encoder re-ranking...")
                    st.write(f"Top {len(results)} re-ranked results:")
                    
                    for res in results:
                        score = res.get("score", 0)
                        metadata = res.get("metadata", {})
                        content = res.get("page_content", "")
                        
                        with st.expander(f"Rerank Score: {score:.4f} | {metadata.get('file_name', 'Unknown')}"):
                            st.json(metadata)
                            st.text_area("Content", value=content, height=150, disabled=True, label_visibility="collapsed")
                except requests.exceptions.RequestException as e:
                    st.error(f"Search failed: {e}")
        
        st.markdown("---")
        st.subheader("Stored Document Chunks")
        with st.expander("View all chunks in PGVector (Main & Delta)", expanded=False):
            df_data = []
            try:
                main_res = requests.get(f"{ADMIN_URL}/pgvector/documents", params={"collection_name": MAIN_COLLECTION, "limit": 100})
                if main_res.status_code == 200:
                    for doc in main_res.json().get("documents", []):
                        doc["Index"] = "Main"
                        doc["Content"] = doc["Content"][:200] + "..." if len(doc["Content"]) > 200 else doc["Content"]
                        df_data.append(doc)
                        
                delta_res = requests.get(f"{ADMIN_URL}/pgvector/documents", params={"collection_name": DELTA_COLLECTION, "limit": 100})
                if delta_res.status_code == 200:
                    for doc in delta_res.json().get("documents", []):
                        doc["Index"] = "Delta"
                        doc["Content"] = doc["Content"][:200] + "..." if len(doc["Content"]) > 200 else doc["Content"]
                        df_data.append(doc)
            except Exception as e:
                st.error(f"Failed to fetch documents: {e}")

            if df_data:
                import pandas as pd
                st.write(f"Showing up to {len(df_data)} chunks:")
                st.dataframe(pd.DataFrame(df_data))
            else:
                st.write("No documents found in database.")
        
        st.markdown("---")
        st.subheader("Manage Vector Store")
        
        col1, col2 = st.columns(2)
        with col1:
            with st.form("add_chunk_form"):
                st.write("**Add New Chunk**")
                new_content = st.text_area("Content (Markdown/Text)")
                new_file = st.text_input("File Name", value="manual_entry")
                new_section = st.text_input("Section Title", value="Manual Insertion")
                if st.form_submit_button("Add Chunk"):
                    if not new_content.strip():
                        st.error("Content cannot be empty.")
                    else:
                        try:
                            res = requests.post(f"{INGESTION_URL}/add_chunk", json={
                                "content": new_content,
                                "file_name": new_file,
                                "section_title": new_section
                            })
                            res.raise_for_status()
                            st.success(res.json().get("message", "Success"))
                            st.rerun()
                        except requests.exceptions.HTTPError as he:
                            st.error(f"HTTP Error: {he.response.text}")
                        except Exception as e:
                            st.error(f"Failed to add chunk: {e}")
                            
        with col2:
            with st.form("delete_chunk_form"):
                st.write("**Delete Chunk**")
                del_doc_id = st.text_input("Document ID")
                del_index = st.selectbox("Index Type", ["Delta", "Main"])
                if st.form_submit_button("Delete Chunk"):
                    if not del_doc_id.strip():
                        st.error("Document ID cannot be empty.")
                    else:
                        try:
                            res = requests.post(f"{INGESTION_URL}/delete_chunk", json={
                                "doc_id": del_doc_id.strip(),
                                "index_type": del_index.lower()
                            })
                            res.raise_for_status()
                            st.success(res.json().get("message", "Success"))
                            st.rerun()
                        except requests.exceptions.HTTPError as he:
                            st.error(f"HTTP Error: {he.response.text}")
                        except Exception as e:
                            st.error(f"Failed to delete chunk: {e}")

        st.write("---")
        if st.button("🔄 Rebuild Main Index (Merge Delta)", key="rebuild_main_index_faiss", help="Merge all Delta ingestion updates into the Main HNSW graph."):
            with st.status("Rebuilding Main Index...", expanded=True) as rebuild_status:
                try:
                    res = requests.post(f"{INGESTION_URL}/rebuild_main_index")
                    res.raise_for_status()
                    data = res.json()
                    st.write(data.get("message", "Rebuild successful."))
                    rebuild_status.update(label="Rebuild Complete!", state="complete")
                except requests.exceptions.HTTPError as he:
                    rebuild_status.update(label="Failed to rebuild index", state="error")
                    st.error(f"HTTP Error: {he.response.text}")
                except Exception as e:
                    rebuild_status.update(label="Failed to rebuild index", state="error")
                    st.error(f"Error: {e}")
    except Exception as e:
        st.error(f"Error loading PGVector database UI: {e}")
