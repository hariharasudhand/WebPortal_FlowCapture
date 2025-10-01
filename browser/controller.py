import time
import threading
import os
import platform
import subprocess
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import json 
from datetime import datetime, timezone
from datetime import datetime, timezone, timedelta
import re
from database.graph_db import store_in_neo4j
from database.vector_db import (
    store_in_pgvector,
    append_page_actions  
)
from database.graph_db import update_page_actions
from util.signals import signals
from collections import defaultdict
action_counters = defaultdict(int)
injected_pages = set()

# Global variables
browser = None
capture_thread = None
stop_capturing = False
flows = {}
TARGET_WEBSITE = ""
all_windows = set()
page_action_buffers = {}   
recorder_injected_for = set() 

# Check if browser is still alive
def is_browser_alive():
    global browser
    try:
        if browser is None:
            return False
        browser.current_window_handle
        return True
    except:
        return False

# Find Chrome installation
def find_chrome_executable():
    if platform.system() == "Windows":
        paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
        ]
    elif platform.system() == "Darwin":  # macOS
        paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chrome.app/Contents/MacOS/Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:  # Linux and others
        paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
    
    for path in paths:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    
    if platform.system() != "Windows":
        try:
            chrome_path = subprocess.check_output(["which", "google-chrome"], text=True).strip()
            if chrome_path:
                return chrome_path
        except:
            pass
        try:
            chrome_path = subprocess.check_output(["which", "chromium"], text=True).strip()
            if chrome_path:
                return chrome_path
        except:
            pass
    return None

# Initialize and start browser with Chrome DevTools Protocol enabled
def start_browser():
    global browser, all_windows
    
    if is_browser_alive():
        return True
    
    try:
        chrome_options = Options()
        chrome_options.add_argument("--window-size=1366,768")
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")

        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_experimental_option("detach", True)
        chrome_options.add_argument("--user-data-dir=./chrome_data")
        
        chrome_path = find_chrome_executable()
        if chrome_path:
            print(f"Found Chrome at: {chrome_path}")
            chrome_options.binary_location = chrome_path
        
        service = Service()
        browser = webdriver.Chrome(service=service, options=chrome_options)
        all_windows = {browser.current_window_handle}
        browser.get("about:blank")
        print("Successfully loaded about:blank")
        return True
    except Exception as e:
        print(f"Error starting browser: {e}")
        signals.error.emit("Browser Error", f"Failed to start browser: {e}")
        try:
            print("Trying alternative browser setup...")
            chrome_options = Options()
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            browser = webdriver.Chrome(options=chrome_options)
            all_windows = {browser.current_window_handle}
            browser.get("about:blank")
            print("Alternative setup successful")
            return True
        except Exception as e2:
            print(f"Alternative method also failed: {e2}")
            return False

# Stop browser
def stop_browser():
    global browser, stop_capturing
    stop_capturing = True
    time.sleep(1)
    if browser:
        try:
            browser.quit()
        except Exception as e:
            print(f"Error closing browser: {e}")
        browser = None

# Metadata extraction 
def extract_metadata(html):
    soup = BeautifulSoup(html, "html.parser")
    meta_tags = {}
    for meta in soup.find_all("meta"):
        name = meta.get("name") or meta.get("property")
        content = meta.get("content")
        if name and content:
            meta_tags[name] = content
    title = soup.title.text.strip() if soup.title else "No Title"
    headings = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        headings.append(h.text.strip())
    forms = []
    for form in soup.find_all("form"):
        form_data = {
            "action": form.get("action", ""),
            "method": form.get("method", ""),
            "id": form.get("id", ""),
            "name": form.get("name", ""),
            "class": form.get("class", []),
            "enctype": form.get("enctype", ""),
            "target": form.get("target", ""),
            "fields": []
        }
        for inp in form.find_all("input"):
            field = {
                "name": inp.get("name", ""),
                "type": inp.get("type", "text"),
                "id": inp.get("id", ""),
                "placeholder": inp.get("placeholder", ""),
                "value": inp.get("value", ""),
                "required": inp.has_attr("required"),
                "readonly": inp.has_attr("readonly"),
                "class": inp.get("class", []),
                "max_length": inp.get("maxlength", ""),
                "min_length": inp.get("minlength", ""),
                "pattern": inp.get("pattern", "")
            }
            form_data["fields"].append(field)
        for select in form.find_all("select"):
            options = []
            for option in select.find_all("option"):
                options.append({
                    "value": option.get("value", ""),
                    "text": option.text.strip(),
                    "selected": option.has_attr("selected")
                })
            field = {
                "name": select.get("name", ""),
                "type": "select",
                "id": select.get("id", ""),
                "required": select.has_attr("required"),
                "options": options,
                "multiple": select.has_attr("multiple")
            }
            form_data["fields"].append(field)
        for textarea in form.find_all("textarea"):
            field = {
                "name": textarea.get("name", ""),
                "type": "textarea",
                "id": textarea.get("id", ""),
                "placeholder": textarea.get("placeholder", ""),
                "value": textarea.text.strip(),
                "required": textarea.has_attr("required"),
                "rows": textarea.get("rows", ""),
                "cols": textarea.get("cols", "")
            }
            form_data["fields"].append(field)
        for button in form.find_all("button"):
            field = {
                "name": button.get("name", ""),
                "type": button.get("type", "button"),
                "id": button.get("id", ""),
                "value": button.get("value", ""),
                "text": button.text.strip()
            }
            form_data["fields"].append(field)
        forms.append(form_data)
    standalone_fields = []
    for inp in soup.find_all("input", recursive=False):
        field = {
            "name": inp.get("name", ""),
            "type": inp.get("type", "text"),
            "id": inp.get("id", ""),
            "value": inp.get("value", ""),
            "placement": "standalone"
        }
        standalone_fields.append(field)
    actions = []
    for btn in soup.find_all("button"):
        action = {
            "text": btn.text.strip(),
            "type": btn.get("type", "button"),
            "id": btn.get("id", ""),
            "class": btn.get("class", []),
            "data_attributes": {attr.replace("data-", ""): btn[attr] for attr in btn.attrs if attr.startswith("data-")}
        }
        actions.append(action)
    for a in soup.find_all("a", attrs={"role": "button"}):
        action = {
            "text": a.text.strip(),
            "href": a.get("href", ""),
            "id": a.get("id", ""),
            "class": a.get("class", []),
            "type": "link-button"
        }
        actions.append(action)
    links = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        text = a.text.strip()
        if href and text:
            links[href] = {
                "text": text,
                "title": a.get("title", ""),
                "target": a.get("target", ""),
                "rel": a.get("rel", "")
            }
    scripts = []
    for script in soup.find_all("script"):
        script_type = script.get("type", "")
        if script.string and script_type != "application/ld+json":
            if script.get("src"):
                scripts.append({"src": script.get("src"), "type": script_type})
            else:
                script_content = script.string.strip()
                preview = script_content[:100] + "..." if len(script_content) > 100 else script_content
                scripts.append({"inline": preview, "type": script_type})
    text_content = soup.get_text().strip()
    words = text_content.split()
    summary = " ".join(words[:100]) + ("..." if len(words) > 100 else "")
    return {
        "title": title,
        "meta_tags": meta_tags,
        "headings": headings[:5],
        "forms": forms,
        "fields": standalone_fields,
        "actions": actions,
        "links": links,
        "scripts": scripts,
        "summary": summary
    }

# Record user action (page-level content + metadata)
def record_action(url, metadata, content, referrer=None):
    if url not in flows:
        flows[url] = {"metadata": metadata, "content": content}

    session_id = None
    try:
        from database.history_manager import history_manager
        if history_manager.current_session:
            session_id = history_manager.current_session.id
    except Exception:
        pass

    store_in_neo4j(url, metadata, referrer, session_id)
    store_in_pgvector(url, content, metadata, session_id)

# Injects JS listeners once per page to capture clicks, inputs, and form submits.
def inject_action_listeners(current_url):
    global injected_pages, browser
    if current_url in injected_pages:
        return
    js = r"""
    (function(){
      if (window.__flow_actions_installed__) { return; }
      window.__flow_actions_installed__ = true;
      window.__flow_actions = window.__flow_actions || [];

      function toISOStringUTC(d){ return new Date(d).toISOString(); }

      function computeXPath(el){
        if (!el || el.nodeType !== 1) return '';
        if (el.id) return "//*[@id='" + el.id.replace(/'/g,"\"") + "']";
        var parts = [];
        while (el && el.nodeType === 1 && el !== document){
          var ix = 1;
          var sib = el.previousSibling;
          while (sib){
            if (sib.nodeType === 1 && sib.nodeName === el.nodeName){ ix++; }
            sib = sib.previousSibling;
          }
          parts.unshift(el.nodeName.toLowerCase() + '[' + ix + ']');
          el = el.parentNode;
        }
        return '/' + parts.join('/');
      }

      function nearestFormInfo(el){
        var form = el.closest ? el.closest('form') : null;
        if (!form) return {name:'', xpath:'', id:''};
        var name = form.getAttribute('name') || form.id || '';
        return { name: name, xpath: computeXPath(form), id: form.id || '' };
      }

      function getLabel(el){
        try{
          var id = el.getAttribute && el.getAttribute('id');
          if (id){
            var lbl = document.querySelector("label[for='" + CSS.escape(id) + "']");
            if (lbl && lbl.textContent) return lbl.textContent.trim();
          }
          var aria = el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby'));
          if (aria){ return aria.trim(); }
          var ph = el.getAttribute && el.getAttribute('placeholder');
          if (ph){ return ph.trim(); }
          // attempt label parent
          var p = el.parentElement;
          for (var i=0; i<3 && p; i++, p=p.parentElement){
            if (p.tagName && p.tagName.toLowerCase()==='label' && p.textContent){
              return p.textContent.trim();
            }
          }
          // preceding label sibling
          var prev = el.previousElementSibling;
          if (prev && prev.tagName && prev.tagName.toLowerCase()==='label' && prev.textContent){
            return prev.textContent.trim();
          }
        }catch(e){}
        return el && (el.name || el.id) || '';
      }

      function isSensitive(el){
        var nm = (el.getAttribute && (el.getAttribute('name')||'')).toLowerCase();
        var tp = (el.getAttribute && (el.getAttribute('type')||'')).toLowerCase();
        return tp==='password' || nm.includes('password') || nm.includes('pwd');
      }

      function pushAction(a){
        // enforce shape
        var obj = {
          type: a.type || '',
          field_xpath: a.field_xpath || '',
          field_label: a.field_label || '',
          data: a.hasOwnProperty('data') ? a.data : undefined,
          sensitive: !!a.sensitive,
          form_name: a.form_name || '',
          form_xpath: a.form_xpath || '',
          timestamp: toISOStringUTC(Date.now())
        };
        window.__flow_actions.push(obj);
      }

      document.addEventListener('click', function(e){
        try{
          var el = e.target;
          // normalize to clickable host (button/input[type=submit]/a)
          if (el && el.tagName){
            var tag = el.tagName.toLowerCase();
            if (tag==='span' || tag==='svg' || tag==='path'){
              // bubble up a bit
              var host = el.closest('button, a, input[type=button], input[type=submit]');
              if (host) el = host;
            }
          }
          var info = nearestFormInfo(el||{});
          pushAction({
            type: 'click',
            field_xpath: computeXPath(el),
            field_label: getLabel(el) || (el && el.textContent ? el.textContent.trim() : ''),
            form_name: info.name,
            form_xpath: info.xpath
          });
        }catch(_){}
      }, true);

      function recordInput(el){
        try{
          var info = nearestFormInfo(el||{});
          var sens = isSensitive(el);
          pushAction({
            type: 'enter',
            field_xpath: computeXPath(el),
            field_label: getLabel(el),
            data: sens ? '********' : (el && el.value !== undefined ? String(el.value) : ''),
            sensitive: sens,
            form_name: info.name,
            form_xpath: info.xpath
          });
        }catch(_){}
      }

      document.addEventListener('change', function(e){
        var el = e.target;
        if (!el || !el.tagName) return;
        var t = el.tagName.toLowerCase();
        if (t==='input' || t==='textarea' || t==='select'){ recordInput(el); }
      }, true);

      document.addEventListener('keyup', function(e){
        var el = e.target;
        if (!el || !el.tagName) return;
        var t = el.tagName.toLowerCase();
        if (t==='input' || t==='textarea'){ recordInput(el); }
      }, true);

      document.addEventListener('submit', function(e){
        try{
          var form = e.target;
          pushAction({
            type: 'submit',
            field_xpath: computeXPath(form),
            field_label: form.getAttribute('name') || form.id || 'form',
            form_name: form.getAttribute('name') || form.id || '',
            form_xpath: computeXPath(form)
          });
        }catch(_){}
      }, true);

      // Shadow DOM traversal: bind listeners onto shadow roots encountered
      function bindShadow(root){
        try{
          root.addEventListener('click', function(e){
            var el = e.target;
            var hostInfo = nearestFormInfo(el||{});
            pushAction({
              type: 'click',
              field_xpath: computeXPath(el),
              field_label: (el && el.textContent ? el.textContent.trim() : '') || getLabel(el),
              form_name: hostInfo.name,
              form_xpath: hostInfo.xpath
            });
          }, true);
          root.addEventListener('change', function(e){
            var el = e.target;
            if (!el || !el.tagName) return;
            var tn = el.tagName.toLowerCase();
            if (tn==='input' || tn==='textarea' || tn==='select'){ recordInput(el); }
          }, true);
          root.addEventListener('keyup', function(e){
            var el = e.target;
            if (!el || !el.tagName) return;
            var tn = el.tagName.toLowerCase();
            if (tn==='input' || tn==='textarea'){ recordInput(el); }
          }, true);
        }catch(_){}
      }
      // iterate known shadow hosts
      try{
        document.querySelectorAll('*').forEach(function(n){
          if (n.shadowRoot){ bindShadow(n.shadowRoot); }
        });
      }catch(_){}

      // helper to pull & clear
      window.__flow_pull_and_clear = function(){
        var out = window.__flow_actions.slice();
        window.__flow_actions.length = 0;
        return out;
      };
    })();
    """
    try:
        browser.execute_script(js)
        injected_pages.add(current_url)
        print("✓ Injected action listeners")
    except Exception as e:
        print(f"Failed to inject listeners: {e}")

# Pull any buffered actions accumulated by the page listeners
def get_and_clear_actions():
    try:
        return browser.execute_script("return (window.__flow_pull_and_clear && window.__flow_pull_and_clear()) || [];")
    except Exception:
        return []

# Assign consecutive action_ids per URL (flow)
def assign_action_ids(url, actions):
    out = []
    for a in actions:
        action_counters[url] += 1
        a["action_id"] = action_counters[url]
        a.setdefault("type", "")
        a.setdefault("field_xpath", "")
        a.setdefault("field_label", "")
        a.setdefault("form_name", "")
        a.setdefault("form_xpath", "")
        a.setdefault("timestamp", "")
        out.append(a)
    return out

# Persist page_actions JSON array to Postgres (append semantics)
def persist_actions(url, actions_with_ids):
    if not actions_with_ids:
        return
    try:
        append_page_actions(url, {"actions": actions_with_ids})
        print(f"✓ Appended {len(actions_with_ids)} actions for {url}")
        try:
            update_page_actions(url, {"actions": actions_with_ids})
            print(f"✓ Mirrored {len(actions_with_ids)} actions to Neo4j for {url}")
        except Exception as _e:
            print(f"Neo4j page_actions mirror failed: {_e}")

    except Exception as e:
        print(f"Failed to append actions: {e}")



# Capture all tabs and windows (unchanged)
def capture_all_tabs():
    global browser, all_windows
    
    current_handle = browser.current_window_handle
    current_url = browser.current_url
    results = []
    try:
        current_handles = set(browser.window_handles)
        new_handles = current_handles - all_windows
        all_windows = current_handles
        for handle in browser.window_handles:
            try:
                browser.switch_to.window(handle)
                url = browser.current_url
                html_content = browser.page_source
                if url == "about:blank":
                    continue
                results.append({
                    "url": url,
                    "content": html_content,
                    "is_new": handle in new_handles
                })
            except Exception as e:
                print(f"Error capturing tab {handle}: {e}")
    except Exception as e:
        print(f"Error in tab capture: {e}")
    finally:
        try:
            browser.switch_to.window(current_handle)
        except:
            if browser.window_handles:
                browser.switch_to.window(browser.window_handles[0])
    return results, current_url

# Check and process alerts/popups (unchanged)
def check_alerts():
    global browser
    try:
        alert = WebDriverWait(browser, 0.5).until(EC.alert_is_present())
        alert_text = alert.text
        url = browser.current_url
        alert_content = f"<html><body><h1>Alert on {url}</h1><p>{alert_text}</p></body></html>"
        alert_metadata = {
            "title": f"Alert on {url}",
            "meta_tags": {},
            "headings": ["Alert"],
            "fields": [],
            "actions": ["OK", "Cancel"],
            "forms": [],
            "links": {},
            "summary": alert_text,
            "is_alert": True
        }
        alert_url = f"{url}#alert-{int(time.time())}"
        record_action(alert_url, alert_metadata, alert_content, url)
        signals.update_status.emit(f"Captured alert: {alert_text[:30]}...")
        alert.accept()
        return True
    except:
        return False

def _inject_action_recorder():
    js = r"""
(function () {
  if (window.__FLOW_RECORDER_INSTALLED__) return "already";
  window.__FLOW_RECORDER_INSTALLED__ = true;

  function toXPath(el) {
    if (!el) return "";
    if (el.id) return "//*[@id='" + el.id.replace(/'/g,"\\'") + "']";
    const parts = [];
    while (el && el.nodeType === Node.ELEMENT_NODE && el !== document) {
      let ix = 0, sib = el.previousSibling;
      while (sib) { if (sib.nodeType === Node.ELEMENT_NODE && sib.nodeName === el.nodeName) ix++; sib = sib.previousSibling; }
      parts.unshift(el.nodeName.toLowerCase() + "[" + (ix + 1) + "]");
      el = el.parentNode;
    }
    return "/" + parts.join("/");
  }

  function safeText(n) {
    if (!n) return "";
    const t = (n.textContent || "").trim();
    return t.replace(/\s+/g, " ");
  }

  function textFromId(id) {
    try {
      if (!id) return "";
      const target = document.getElementById(id);
      return safeText(target);
    } catch (_) { return ""; }
  }

  function nearestForm(el) {
    if (!el || !el.closest) return null;
    return el.closest("form");
  }

  function formFriendlyName(f) {
    if (!f) return "";
    // Prefer name/id
    const nm = f.getAttribute("name") || f.id;
    if (nm) return nm;
    // Then action path (strip query)
    const act = f.getAttribute("action") || "";
    if (act) {
      try {
        const u = new URL(act, location.href);
        return (u.pathname || "/").replace(/\/+$/, "");
      } catch (_) {
        return act;
      }
    }
    // Then first heading on page as a last resort
    const h = document.querySelector("h1,h2,h3");
    if (h) return safeText(h);
    return "form";
  }

  function labelFor(el) {
    try {
      if (!el) return "";

      // 1) explicit label[for=id]
      const id = el.getAttribute("id");
      if (id) {
        const explicit = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (explicit) {
          const t = safeText(explicit);
          if (t) return t;
        }
      }

      // 2) aria-labelledby
      const ariaLblBy = el.getAttribute("aria-labelledby");
      if (ariaLblBy) {
        // could be space-separated ids
        const ids = ariaLblBy.split(/\s+/).filter(Boolean);
        const combined = ids.map(textFromId).filter(Boolean).join(" ");
        if (combined) return combined.trim();
      }

      // 3) aria-label
      const aria = el.getAttribute("aria-label");
      if (aria && aria.trim()) return aria.trim();

      // 4) placeholder / title
      const ph = el.getAttribute("placeholder");
      if (ph && ph.trim()) return ph.trim();
      const ti = el.getAttribute("title");
      if (ti && ti.trim()) return ti.trim();

      // 5) wrapping/adjacent label
      let p = el.parentElement;
      for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
        if (p.tagName && p.tagName.toLowerCase() === "label") {
          const t = safeText(p);
          if (t) return t;
        }
      }
      const prev = el.previousElementSibling;
      if (prev && prev.tagName && prev.tagName.toLowerCase() === "label") {
        const t = safeText(prev);
        if (t) return t;
      }

      // 6) button/link text
      const tag = (el.tagName || "").toLowerCase();
      if (tag === "button" || el.getAttribute("role") === "button" || tag === "a") {
        const t = safeText(el);
        if (t) return t;
      }

      // 7) finally, name/id
      return el.getAttribute("name") || id || "";
    } catch (_) {
      return el && (el.getAttribute && (el.getAttribute("name") || el.getAttribute("id"))) || "";
    }
  }

  function nowIso() { return new Date().toISOString().replace(/\.\d+Z$/,"Z"); }

  window.__RECORDED_ACTIONS__ = [];
  const lastInput = new Map(); // xpath -> {t, obj}

  function pushAction(obj) { window.__RECORDED_ACTIONS__.push(obj); }

  function handleEnter(el) {
    const xpath = toXPath(el);
    const t = Date.now();
    const sensitive = (() => {
      const s = ((el.getAttribute("name")||"") + " " + (el.getAttribute("type")||"") + " " + (el.getAttribute("id")||"")).toLowerCase();
      return /password|pass|pwd|otp|ssn|card|cvv/.test(s);
    })();
    const data = sensitive ? "********" : (el.value ?? "");
    const prev = lastInput.get(xpath);
    const f = nearestForm(el);
    const obj = {
      _type: "enter",
      field_xpath: xpath,
      field_label: labelFor(el),
      data: data,
      sensitive: !!sensitive,
      form_name: formFriendlyName(f),
      form_xpath: f ? toXPath(f) : "",
      timestamp: nowIso()
    };

    if (prev && (t - prev.t) < 600) {
      prev.obj.data = data;
      prev.obj.field_label = prev.obj.field_label || obj.field_label;
      prev.obj.form_name = prev.obj.form_name || obj.form_name;
      prev.obj.form_xpath = prev.obj.form_xpath || obj.form_xpath;
      prev.obj.timestamp = nowIso();
      prev.t = t;
      return;
    }
    lastInput.set(xpath, {t, obj});
    pushAction(obj);
  }

  document.addEventListener("click", function (e) {
    const el = e.target && e.target.closest && e.target.closest("button, a, input[type=submit], input[type=button], [role=button], input[type=checkbox], input[type=radio]");
    if (!el) return;
    const f = nearestForm(el);
    pushAction({
      _type: "click",
      field_xpath: toXPath(el),
      field_label: labelFor(el),
      data: null,
      sensitive: false,
      form_name: formFriendlyName(f),
      form_xpath: f ? toXPath(f) : "",
      timestamp: nowIso()
    });
  }, true);

  document.addEventListener("change", function (e) {
    const el = e.target;
    if (!el || !(el instanceof HTMLElement)) return;
    if (["INPUT","TEXTAREA","SELECT"].includes(el.tagName)) handleEnter(el);
  }, true);

  // Only commit on Enter key
  document.addEventListener("keyup", function (e) {
    const el = e.target;
    if (!el || !(el instanceof HTMLElement)) return;
    if (["INPUT","TEXTAREA"].includes(el.tagName) && e.key === "Enter") {
      handleEnter(el);
    }
  }, true);

  // Also commit when leaving the field
  document.addEventListener("blur", function (e) {
    const el = e.target;
    if (!el || !(el instanceof HTMLElement)) return;
    if (["INPUT","TEXTAREA"].includes(el.tagName)) {
      handleEnter(el);
    }
  }, true);


  document.addEventListener("submit", function (e) {
    const f = e.target;
    if (!f) return;
    pushAction({
      _type: "submit",
      field_xpath: toXPath(f),
      field_label: formFriendlyName(f),
      data: null,
      sensitive: false,
      form_name: formFriendlyName(f),
      form_xpath: toXPath(f),
      timestamp: nowIso()
    });
  }, true);

  return "installed";
})();
"""
    try:
        browser.execute_script(js)
    except Exception:
        pass


def _drain_actions_from_page():
    """Fetch & clear buffered actions from the page JS recorder."""
    js = "var a=(window.__RECORDED_ACTIONS__||[]).splice(0,(window.__RECORDED_ACTIONS__||[]).length); return a;"
    try:
        return browser.execute_script(js) or []
    except Exception:
        return []


def _append_actions(url, raw_actions):
    """
    Assign auto-incrementing action_id per URL and append to buffer.
    """
    if url not in page_action_buffers:
        page_action_buffers[url] = {"actions": [], "last_id": 0}
    buf = page_action_buffers[url]
    for a in raw_actions:
        buf["last_id"] += 1
        a["action_id"] = buf["last_id"]
        a["type"] = a.pop("_type", a.get("type", ""))
        buf["actions"].append(a)


def _iso_to_dt(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None

def _label_from_xpath(xp: str) -> str:
    m = re.search(r"@\s*id\s*=\s*'([^']+)'", xp or "")
    if m:
        return m.group(1)
    segs = (xp or "").split('/')
    return segs[-1] if segs else ""

def _normalize_actions_for_url(url: str, raw_actions: list, existing_actions: list):
    """
    Normalize a batch of actions BEFORE saving:
      - merge rapid repeats of 'enter' on the same field (keep latest value)
      - collapse duplicate clicks within 600ms on the same control
      - backfill missing labels from xpath/id
      - backfill missing form_name/form_xpath from most recent context
    Returns actions WITHOUT action_id; caller assigns ids.
    """
    last_label_by_xpath = {}
    last_form_by_xpath  = {}
    last_form_seen      = {"name": "", "xpath": ""}

    for a in existing_actions or []:
        xp = a.get("field_xpath") or ""
        if a.get("field_label"): last_label_by_xpath[xp] = a["field_label"]
        if a.get("form_xpath"):
            last_form_by_xpath[xp] = (a.get("form_name",""), a["form_xpath"])
            last_form_seen = {"name": a.get("form_name",""), "xpath": a["form_xpath"]}

    def ts(a):
        return _iso_to_dt(a.get("timestamp","")) or datetime.now(timezone.utc)

    new_sorted = sorted(raw_actions or [], key=ts)
    out = []

    ENTER_MERGE_WINDOW = timedelta(seconds=2)
    CLICK_MERGE_WINDOW = timedelta(milliseconds=600)

    for a in new_sorted:
        typ   = (a.get("type") or a.get("_type") or "").lower()
        xp    = a.get("field_xpath","") or ""
        label = (a.get("field_label") or "").strip()
        data  = a.get("data", None)
        sens  = bool(a.get("sensitive", False))
        fname = (a.get("form_name") or "").strip()
        fxp   = (a.get("form_xpath") or "").strip()
        t     = _iso_to_dt(a.get("timestamp","")) or datetime.now(timezone.utc)

        if not label:
            label = last_label_by_xpath.get(xp) or _label_from_xpath(xp)

        if not fxp and last_form_by_xpath.get(xp):
            fname, fxp = last_form_by_xpath[xp]
        if not fxp and last_form_seen["xpath"]:
            fname = fname or last_form_seen["name"]
            fxp   = last_form_seen["xpath"]

        if label: last_label_by_xpath[xp] = label
        if fxp:
            last_form_by_xpath[xp] = (fname, fxp)
            last_form_seen = {"name": fname, "xpath": fxp}

        if typ == "enter" and sens:
            data = "********"

        candidate = {
            "type": typ,
            "field_xpath": xp,
            "field_label": label,
            "data": data if typ == "enter" else None,
            "sensitive": sens,
            "form_name": fname,
            "form_xpath": fxp,
            "timestamp": t.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z'),
        }

        if typ == "enter":
            if out and out[-1]["type"] == "enter" and out[-1]["field_xpath"] == xp:
                t_prev = _iso_to_dt(out[-1]["timestamp"]) or t
                if (t - t_prev) <= ENTER_MERGE_WINDOW:
                    out[-1]["data"] = candidate["data"]
                    out[-1]["field_label"] = out[-1]["field_label"] or candidate["field_label"]
                    out[-1]["form_name"] = out[-1]["form_name"] or candidate["form_name"]
                    out[-1]["form_xpath"] = out[-1]["form_xpath"] or candidate["form_xpath"]
                    out[-1]["timestamp"] = candidate["timestamp"]
                    out[-1]["sensitive"] = out[-1]["sensitive"] or candidate["sensitive"]
                    continue

        if typ == "click":
            if out and out[-1]["type"] == "click" and out[-1]["field_xpath"] == xp:
                t_prev = _iso_to_dt(out[-1]["timestamp"]) or t
                if (t - t_prev) <= CLICK_MERGE_WINDOW:
                    out[-1]["timestamp"] = candidate["timestamp"]
                    if candidate["form_xpath"] and not out[-1]["form_xpath"]:
                        out[-1]["form_xpath"] = candidate["form_xpath"]
                        out[-1]["form_name"]  = candidate["form_name"]
                    continue

        out.append(candidate)

    return out



def _persist_page_actions_if_any(url, metadata):
    from database.vector_db import store_in_pgvector
    if url not in page_action_buffers:
        return
    actions = page_action_buffers[url]["actions"]
    if not actions:
        return

    session_id = None
    try:
        from database.history_manager import history_manager
        if history_manager.current_session:
            session_id = history_manager.current_session.id
    except Exception:
        pass

    store_in_pgvector(
        url=url,
        content=flows.get(url, {}).get("content", ""),
        metadata=metadata,
        session_id=session_id,
        page_actions={"actions": actions}
    )

    try:
        update_page_actions(url, {"actions": actions})
        print(f"✓ Mirrored buffered {len(actions)} actions to Neo4j for {url}")
    except Exception as _e:
        print(f"Neo4j page_actions mirror failed: {_e}")




# Continuously capture web actions
def capture_web_actions():
    global browser, stop_capturing, TARGET_WEBSITE, all_windows
    
    if not TARGET_WEBSITE.startswith(('http://', 'https://')):
        TARGET_WEBSITE = 'https://' + TARGET_WEBSITE
    
    try:
        if browser is None or not is_browser_alive():
            signals.error.emit("Browser Error", "Browser is not running. Please start the browser first.")
            return
            
        browser.get(TARGET_WEBSITE)

        _inject_action_recorder()
        signals.update_status.emit(f"Opened target website: {TARGET_WEBSITE}")
        print(f"✓ Opened target website: {TARGET_WEBSITE}")
        
        # Store initial page
        url = browser.current_url
        html_content = browser.page_source
        metadata = extract_metadata(html_content)
        record_action(url, metadata, html_content)

        last_url = url
        last_content_hash = hash(html_content)
        processed_urls = {url: last_content_hash}
        
        while not stop_capturing:
            if not is_browser_alive():
                signals.error.emit("Browser Error", "Browser window was closed")
                break

            try:
                _inject_action_recorder() 
                drained = _drain_actions_from_page()
                if drained:
                    current_url = browser.current_url
                    existing = page_action_buffers.get(current_url, {}).get("actions", [])
                    cleaned = _normalize_actions_for_url(current_url, drained, existing)

                    # assign fresh incremental ids and buffer
                    if current_url not in page_action_buffers:
                        page_action_buffers[current_url] = {"actions": [], "last_id": 0}
                    buf = page_action_buffers[current_url]
                    for a in cleaned:
                        buf["last_id"] += 1
                        a["action_id"] = buf["last_id"]
                        buf["actions"].append(a)

                    _persist_page_actions_if_any(current_url, extract_metadata(browser.page_source))
                    signals.update_status.emit(f"Captured {len(cleaned)} action(s) after normalization")
            except Exception:
                pass

            try:
                tabs_data, current_url = capture_all_tabs()
                for tab_data in tabs_data:
                    tab_url = tab_data["url"]
                    html_content = tab_data["content"]
                    content_hash = hash(html_content)
                    
                    if tab_url not in processed_urls or processed_urls[tab_url] != content_hash:
                        metadata = extract_metadata(html_content)
                        referrer = None
                        if tab_data.get("is_new", False):
                            referrer = last_url
                        elif tab_url in processed_urls:
                            referrer = tab_url
                        
                        record_action(tab_url, metadata, html_content, referrer)

                        try:
                            _inject_action_recorder()
                        except Exception:
                            pass
                        
                        processed_urls[tab_url] = content_hash
                
                if current_url != last_url:
                    last_url = current_url
                
            except Exception as e:
                print(f"Temporary error in capture loop: {e}")
                time.sleep(1)
                if not is_browser_alive():
                    break

            time.sleep(0.3)  
                
    except Exception as e:
        print(f"Error in capture thread: {e}")
        signals.error.emit("Capture Error", f"Error capturing web actions: {e}")
    
    signals.update_status.emit("Capture thread stopped")


# Start the capture process
def start_capturing(target_url):
    global TARGET_WEBSITE, stop_capturing, capture_thread, all_windows, injected_pages
    
    TARGET_WEBSITE = target_url
    stop_capturing = False
    injected_pages = set()  
    
    if not TARGET_WEBSITE.startswith(('http://', 'https://')):
        TARGET_WEBSITE = 'https://' + TARGET_WEBSITE
    
    try:
        all_windows = set(browser.window_handles)
    except Exception as e:
        print(f"Error getting window handles: {e}")
        all_windows = set()
    
    try:
        browser.get(TARGET_WEBSITE)
        print(f"Successfully navigated to {TARGET_WEBSITE}")
    except Exception as e:
        print(f"Error navigating to {TARGET_WEBSITE}: {e}")
        signals.error.emit("Navigation Error", f"Could not navigate to {TARGET_WEBSITE}: {e}")
        return None
    
    capture_thread = threading.Thread(target=capture_web_actions, daemon=True)
    capture_thread.start()
    return capture_thread

# Stop the capture process
def stop_capturing_process():
    global stop_capturing
    stop_capturing = True
    if capture_thread and capture_thread.is_alive():
        capture_thread.join(timeout=2)