import sys
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import numpy as np
from sentence_transformers import SentenceTransformer
import subprocess
import os
import threading
import time
import networkx as nx
import matplotlib.pyplot as plt
from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout, QMessageBox, QCheckBox, QTextEdit, QScrollArea
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, Qt
from PyQt5.QtGui import QFont
import warnings
import json
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from datetime import datetime

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import json
import csv
import json
from database.graph_db import store_in_neo4j as store_in_neo4j_core
from database.graph_db import update_page_actions as update_page_actions_core
from database.vector_db import store_in_pgvector as store_in_pgvector_core
from database.vector_db import append_page_actions as append_page_actions_core
import datetime



action_counters = {}  

def _common_url(u: str) -> str:
    try:
        host = urlparse(u).netloc or ""
        return host.lower()
    except Exception:
        return ""


def _install_action_listeners(browser):
    """Inject once per page. Captures clicks & data entry with labels, xpaths, form info."""
    js = r"""
    (function () {
    if (window.__wf_installed) return;
    window.__wf_installed = true;
    window.__actionLog = [];

    function iso() { return new Date().toISOString().replace(/\.\d+Z$/,'Z'); }

    function getXPath(el) {
        if (!el || el.nodeType !== 1) return "";
        if (el.id) return "//*[@id='" + el.id.replace(/'/g,"\\'") + "']";
        var parts = [];
        while (el && el.nodeType === 1 && el !== document) {
        var ix = 1, sib = el.previousSibling;
        while (sib) { if (sib.nodeType === 1 && sib.nodeName === el.nodeName) ix++; sib = sib.previousSibling; }
        parts.unshift(el.nodeName.toLowerCase() + "[" + ix + "]");
        el = el.parentNode;
        }
        return "/" + parts.join("/");
    }

    function formInfo(el) {
        var f = el && el.closest ? el.closest("form") : null;
        if (!f) return {name: "", xpath: ""};
        var name = (f.getAttribute("name") || f.id || "").trim();
        return {name: name, xpath: getXPath(f)};
    }

    function labelFor(el) {
        try {
        if (!el) return "";
        var id = el.getAttribute && el.getAttribute("id");
        if (id) {
            var l = document.querySelector('label[for="'+ CSS.escape(id) +'"]');
            if (l && l.innerText) return l.innerText.trim();
        }
        if (el.labels && el.labels.length) return (el.labels[0].innerText || "").trim();
        var aria = (el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || "").trim();
        if (aria) return aria;
        var ph = (el.getAttribute("placeholder") || "").trim();
        if (ph) return ph;
        var tag = (el.tagName || "").toLowerCase();
        if (tag==="button" || el.getAttribute("role")==="button") {
            var t = (el.innerText || el.value || "").trim();
            if (t) return t;
        }
        var prev = el.previousElementSibling;
        if (prev && prev.tagName==="LABEL") return (prev.innerText || "").trim();
        } catch(e){}
        return (el && (el.name || el.id)) || "";
    }

    function isSensitive(el) {
        var t = ((el && el.type) || "").toLowerCase();
        var n = ((el && (el.name||"")) + " " + (el && (el.id||"")) + " " + (el && (el.getAttribute && el.getAttribute("placeholder") || ""))).toLowerCase();
        return t==="password" || /pass|pwd|otp|token|secret|ssn|card|cvv|pin/.test(n);
    }

    // push one normalized action
    function push(type, el, val) {
        if (!el) return;
        var fi = formInfo(el);
        var sens = isSensitive(el);
        window.__actionLog.push({
        type: type,
        field_label: labelFor(el),
        field_xpath: getXPath(el),
        form_name: fi.name || "",
        form_xpath: fi.xpath || "",
        data: (type==="enter") ? (sens ? "********" : (val==null ? "" : String(val))) : null,
        sensitive: !!sens,
        timestamp: iso()
        });
    }

    // clicks
    document.addEventListener("click", function(e){
        var el = e.target && e.target.closest && e.target.closest("button, a, input[type='button'], input[type='submit']");
        if (el) push("click", el, null);
    }, true);

    // commit text on Enter
    document.addEventListener("keyup", function(e){
        var el = e.target;
        if (!el || !(el.matches && el.matches("input, textarea"))) return;
        if (e.key === "Enter") push("enter", el, el.value);
    }, true);

    // commit text when leaving the field
    document.addEventListener("blur", function(e){
        var el = e.target;
        if (!el || !(el.matches && el.matches("input, textarea, select"))) return;
        var v = (el.tagName==="SELECT") ? el.value : el.value;
        push("enter", el, v);
    }, true);
    })();
    """
    try:
        browser.execute_script(js)
    except Exception as e:
        print(f"Could not install action listeners: {e}")


def _drain_actions(browser):
    """Pull and clear the JS-side buffer; return list of action dicts."""
    try:
        actions = browser.execute_script("var a = window.__actionLog || []; window.__actionLog = []; return a;")
        return actions or []
    except Exception:
        return []


def extract_shadow_dom_fields(browser):
    """Extract fields from shadow DOM elements using JavaScript"""
    shadow_fields = []
    try:
        script = """
        var fields = [];
        
        // Function to recursively search shadow DOM
        function searchShadowDOM(root) {
            // Search current level
            root.querySelectorAll('input, textarea, select, button').forEach(function(field) {
                var fieldData = {
                    type: field.type || 'text',
                    id: field.id || '',
                    name: field.name || '',
                    placeholder: field.placeholder || '',
                    className: field.className || '',
                    tagName: field.tagName.toLowerCase(),
                    value: field.value || '',
                    required: field.required || false,
                    disabled: field.disabled || false,
                    ariaLabel: field.getAttribute('aria-label') || '',
                    dataAttributes: {}
                };
                
                // Get data-* attributes
                for (var i = 0; i < field.attributes.length; i++) {
                    var attr = field.attributes[i];
                    if (attr.name.startsWith('data-')) {
                        fieldData.dataAttributes[attr.name] = attr.value;
                    }
                }
                
                // Add text content for buttons
                if (field.tagName.toLowerCase() === 'button') {
                    fieldData.text = field.textContent.trim();
                }
                
                fields.push(fieldData);
            });
            
            // Search shadow roots
            root.querySelectorAll('*').forEach(function(el) {
                if (el.shadowRoot) {
                    searchShadowDOM(el.shadowRoot);
                }
            });
        }
        
        // Start search from document
        searchShadowDOM(document);
        
        return fields;
        """

        shadow_fields = browser.execute_script(script)
        print(f"Found {len(shadow_fields)} shadow DOM fields")

    except Exception as e:
        print(f"Could not extract shadow DOM fields: {e}")

    return shadow_fields


def extract_dynamic_fields_selenium(browser):
    """Extract fields using Selenium direct DOM access with enhanced detection"""
    fields = []

    try:
        # Wait for page to fully load
        WebDriverWait(browser, 10).until(
            lambda driver: driver.execute_script(
                "return document.readyState") == "complete")

        # Get all input elements
        inputs = browser.find_elements(By.TAG_NAME, "input")
        textareas = browser.find_elements(By.TAG_NAME, "textarea")
        selects = browser.find_elements(By.TAG_NAME, "select")
        buttons = browser.find_elements(By.TAG_NAME, "button")

        print(
            f"Selenium found: {len(inputs)} inputs, {len(textareas)} textareas, {len(selects)} selects, {len(buttons)} buttons")

        # Process input elements
        for i, input_elem in enumerate(inputs):
            try:
                field_data = {
                    "element_type": "input",
                    "type": input_elem.get_attribute("type") or "text",
                    "name": input_elem.get_attribute("name") or "",
                    "id": input_elem.get_attribute("id") or "",
                    "placeholder": input_elem.get_attribute("placeholder") or "",
                    "class": input_elem.get_attribute("class") or "",
                    "value": input_elem.get_attribute("value") or "",
                    "required": input_elem.get_attribute("required") is not None,
                    "disabled": not input_elem.is_enabled(),
                    "displayed": input_elem.is_displayed(),
                    "aria_label": input_elem.get_attribute("aria-label") or "",
                    "autocomplete": input_elem.get_attribute("autocomplete") or "",
                    "form": input_elem.get_attribute("form") or "",
                    "xpath": f"//input[{i+1}]",
                    "css_selector": generate_css_selector(input_elem),
                    "data_attributes": extract_data_attributes(input_elem)
                }

                fields.append(field_data)

            except Exception as e:
                print(f"Error processing input {i}: {e}")

        # Process textarea elements
        for i, textarea in enumerate(textareas):
            try:
                field_data = {
                    "element_type": "textarea",
                    "type": "textarea",
                    "name": textarea.get_attribute("name") or "",
                    "id": textarea.get_attribute("id") or "",
                    "placeholder": textarea.get_attribute("placeholder") or "",
                    "class": textarea.get_attribute("class") or "",
                    "value": textarea.get_attribute("value") or textarea.text,
                    "required": textarea.get_attribute("required") is not None,
                    "disabled": not textarea.is_enabled(),
                    "displayed": textarea.is_displayed(),
                    "rows": textarea.get_attribute("rows") or "",
                    "cols": textarea.get_attribute("cols") or "",
                    "css_selector": generate_css_selector(textarea),
                    "data_attributes": extract_data_attributes(textarea)
                }

                fields.append(field_data)

            except Exception as e:
                print(f"Error processing textarea {i}: {e}")

        # Process select elements
        for i, select in enumerate(selects):
            try:
                options = []
                try:
                    option_elements = select.find_elements(
                        By.TAG_NAME, "option")
                    for opt in option_elements:
                        options.append({
                            "value": opt.get_attribute("value") or "",
                            "text": opt.text.strip(),
                            "selected": opt.is_selected()
                        })
                except:
                    pass

                field_data = {
                    "element_type": "select",
                    "type": "select",
                    "name": select.get_attribute("name") or "",
                    "id": select.get_attribute("id") or "",
                    "class": select.get_attribute("class") or "",
                    "required": select.get_attribute("required") is not None,
                    "disabled": not select.is_enabled(),
                    "displayed": select.is_displayed(),
                    "multiple": select.get_attribute("multiple") is not None,
                    "options": options,
                    "options_count": len(options),
                    "css_selector": generate_css_selector(select),
                    "data_attributes": extract_data_attributes(select)
                }

                fields.append(field_data)

            except Exception as e:
                print(f"Error processing select {i}: {e}")

        # Process button elements
        for i, button in enumerate(buttons):
            try:
                field_data = {
                    "element_type": "button",
                    "type": f"button_{button.get_attribute('type') or 'button'}",
                    "name": button.get_attribute("name") or "",
                    "id": button.get_attribute("id") or "",
                    "class": button.get_attribute("class") or "",
                    "text": button.text.strip(),
                    "value": button.get_attribute("value") or "",
                    "disabled": not button.is_enabled(),
                    "displayed": button.is_displayed(),
                    "onclick": button.get_attribute("onclick") or "",
                    "form": button.get_attribute("form") or "",
                    "css_selector": generate_css_selector(button),
                    "data_attributes": extract_data_attributes(button)
                }

                fields.append(field_data)

            except Exception as e:
                print(f"Error processing button {i}: {e}")

    except Exception as e:
        print(f"Error in Selenium field extraction: {e}")

    return fields


def extract_data_attributes(element):
    """Extract all data-* attributes from an element"""
    data_attrs = {}
    try:
        # Get all attributes using JavaScript
        script = """
        var attrs = {};
        for (var i = 0; i < arguments[0].attributes.length; i++) {
            var attr = arguments[0].attributes[i];
            if (attr.name.startsWith('data-')) {
                attrs[attr.name] = attr.value;
            }
        }
        return attrs;
        """
        data_attrs = browser.execute_script(script, element)
    except:
        pass

    return data_attrs


def generate_css_selector(element):
    """Generate a reliable CSS selector for an element"""
    selectors = []

    try:
        # Try ID first 
        element_id = element.get_attribute("id")
        if element_id:
            selectors.append(f"#{element_id}")

        # Try name attribute
        name = element.get_attribute("name")
        tag_name = element.tag_name.lower()
        if name:
            selectors.append(f"{tag_name}[name='{name}']")

        # Try placeholder
        placeholder = element.get_attribute("placeholder")
        if placeholder:
            selectors.append(f"{tag_name}[placeholder='{placeholder}']")

        # Try type and class combination
        field_type = element.get_attribute("type")
        class_name = element.get_attribute("class")
        if field_type and class_name:
            selectors.append(
                f"{tag_name}[type='{field_type}'].{class_name.split()[0]}")
        elif field_type:
            selectors.append(f"{tag_name}[type='{field_type}']")

        # Try aria-label
        aria_label = element.get_attribute("aria-label")
        if aria_label:
            selectors.append(f"{tag_name}[aria-label='{aria_label}']")

        # For buttons, try text content
        if tag_name == "button":
            text = element.text.strip()
            if text:
                selectors.append(f"button:contains('{text}')")

        # Generate xpath as fallback
        try:
            xpath = element.parent.execute_script("""
                function getXPath(element) {
                    if (element.id !== '') {
                        return "//*[@id='" + element.id + "']";
                    }
                    if (element === document.body) {
                        return '/html/body';
                    }
                    var ix = 0;
                    var siblings = element.parentNode.childNodes;
                    for (var i = 0; i < siblings.length; i++) {
                        var sibling = siblings[i];
                        if (sibling === element) {
                            return getXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                        }
                        if (sibling.nodeType === 1 && sibling.tagName === element.tagName) {
                            ix++;
                        }
                    }
                }
                return getXPath(arguments[0]);
            """, element)
            if xpath:
                selectors.append(f"xpath: {xpath}")
        except:
            pass

    except Exception as e:
        print(f"Error generating CSS selector: {e}")

    return selectors[:3]  


def extract_forms_with_selenium(browser):
    """Extract forms with Selenium, including all nested controls + attributes."""
    forms_data = []
    try:
        form_elements = browser.find_elements(By.TAG_NAME, "form")
        for form_idx, form in enumerate(form_elements):
            form_data = {
                "form_index": form_idx,
                "action": form.get_attribute("action") or "",
                "method": (form.get_attribute("method") or "get").upper(),
                "name": form.get_attribute("name") or "",
                "id": form.get_attribute("id") or "",
                "class": form.get_attribute("class") or "",
                "enctype": form.get_attribute("enctype") or "application/x-www-form-urlencoded",
                "target": form.get_attribute("target") or "",
                "autocomplete": form.get_attribute("autocomplete") or "",
                "novalidate": form.get_attribute("novalidate") is not None,
                "css_selector": generate_css_selector(form),
                "fields": []
            }
            controls = form.find_elements(
                By.CSS_SELECTOR, "input, textarea, select, button")
            for control_idx, c in enumerate(controls):
                tag = c.tag_name.lower()
                if tag == "input":
                    fd = {
                        "name": c.get_attribute("name") or "",
                        "type": c.get_attribute("type") or "text",
                        "id": c.get_attribute("id") or "",
                        "placeholder": c.get_attribute("placeholder") or "",
                        "class": c.get_attribute("class") or "",
                        "required": c.get_attribute("required") is not None,
                        "value": c.get_attribute("value") or "",
                        "autocomplete": c.get_attribute("autocomplete") or "",
                        "aria_label": c.get_attribute("aria-label") or "",
                        "onclick": c.get_attribute("onclick") or "",
                        "disabled": not c.is_enabled()
                    }
                elif tag == "textarea":
                    fd = {
                        "name": c.get_attribute("name") or "",
                        "type": "textarea",
                        "id": c.get_attribute("id") or "",
                        "placeholder": c.get_attribute("placeholder") or "",
                        "class": c.get_attribute("class") or "",
                        "required": c.get_attribute("required") is not None,
                        "value": c.get_attribute("value") or c.text,
                        "rows": c.get_attribute("rows") or "",
                        "cols": c.get_attribute("cols") or "",
                        "aria_label": c.get_attribute("aria-label") or "",
                        "disabled": not c.is_enabled()
                    }
                elif tag == "select":
                    options = []
                    for opt in c.find_elements(By.TAG_NAME, "option"):
                        options.append({"value": opt.get_attribute(
                            "value") or "", "text": opt.text.strip(), "selected": opt.is_selected()})
                    fd = {
                        "name": c.get_attribute("name") or "",
                        "type": "select",
                        "id": c.get_attribute("id") or "",
                        "class": c.get_attribute("class") or "",
                        "required": c.get_attribute("required") is not None,
                        "multiple": c.get_attribute("multiple") is not None,
                        "options": options,
                        "options_count": len(options),
                        "aria_label": c.get_attribute("aria-label") or "",
                        "disabled": not c.is_enabled()
                    }
                else:  # button
                    fd = {
                        "name": c.get_attribute("name") or "",
                        "type": f"button_{c.get_attribute('type') or 'button'}",
                        "id": c.get_attribute("id") or "",
                        "class": c.get_attribute("class") or "",
                        "text": c.text.strip(),
                        "value": c.get_attribute("value") or "",
                        "onclick": c.get_attribute("onclick") or "",
                        "disabled": not c.is_enabled()
                    }
                form_data["fields"].append(fd)
            form_data["field_count"] = len(form_data["fields"])
            forms_data.append(form_data)
    except Exception as e:
        print(f"Error in Selenium form extraction: {e}")
    return forms_data


def debug_page_structure(browser):
    """Debug function to analyze page structure"""
    if not browser:
        return

    try:
        print("\n🔍 DEBUG: Analyzing page structure...")

        # Get page info
        title = browser.title
        url = browser.current_url
        print(f"Page: {title} ({url})")

        # Check for dynamic content loading
        script = """
        return {
            readyState: document.readyState,
            formsCount: document.forms.length,
            inputsCount: document.querySelectorAll('input').length,
            buttonsCount: document.querySelectorAll('button').length,
            textareasCount: document.querySelectorAll('textarea').length,
            selectsCount: document.querySelectorAll('select').length,
            hasJQuery: typeof jQuery !== 'undefined',
            hasReact: typeof React !== 'undefined',
            hasAngular: typeof angular !== 'undefined',
            hasVue: typeof Vue !== 'undefined'
        }
        """

        page_info = browser.execute_script(script)
        print(f"Page info: {json.dumps(page_info, indent=2)}")

        # Check for iframe elements
        iframes = browser.find_elements(By.TAG_NAME, "iframe")
        if iframes:
            print(
                f"Found {len(iframes)} iframes - may contain additional forms")

        # Check for modal dialogs
        modals = browser.find_elements(
            By.CSS_SELECTOR, ".modal, [role='dialog'], .popup, .overlay")
        if modals:
            print(f"Found {len(modals)} potential modal elements")

        # Sample some elements for debugging
        inputs = browser.find_elements(By.TAG_NAME, "input")[:5]
        print(f"Sample input elements:")
        for i, inp in enumerate(inputs):
            try:
                print(
                    f"  Input {i}: type={inp.get_attribute('type')}, id={inp.get_attribute('id')}, name={inp.get_attribute('name')}, class={inp.get_attribute('class')}")
            except:
                print(f"  Input {i}: Error getting attributes")

    except Exception as e:
        print(f"Debug analysis error: {e}")


warnings.filterwarnings('ignore', message='Unverified HTTPS request')


flows = {}
field_stats = {}
TARGET_WEBSITE = ""
G = nx.DiGraph()
browser = None
capture_thread = None
stop_capturing = False

# Create a signal class for thread communication


class WorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str, str)
    success = pyqtSignal(str)
    warning = pyqtSignal(str, str)
    update_status = pyqtSignal(str)
    field_stats_updated = pyqtSignal(dict)


# Global signals instance
signals = WorkerSignals()


def extract_enhanced_metadata(html):
    """
    Parse HTML and extract enhanced metadata:
      - fields: inputs, textareas, selects, buttons, links, divs, spans, lists
      - forms: each with all nested controls (inputs/selects/textareas/buttons) + attributes
      - title, headings
    """
    soup = BeautifulSoup(html or "", "html.parser")

    def cls(elem):
        return " ".join(elem.get("class", [])) if elem and elem.get("class") else ""

    fields = []

    # INPUTS
    for tag in soup.find_all("input"):
        fields.append({
            "element_type": "input",
            "tag": "input",
            "type": tag.get("type", "text"),
            "name": tag.get("name", "") or "",
            "id": tag.get("id", "") or "",
            "placeholder": tag.get("placeholder", "") or "",
            "class": cls(tag),
            "value": tag.get("value", "") or "",
            "required": tag.has_attr("required"),
            "disabled": tag.has_attr("disabled"),
            "displayed": True,
            "aria_label": tag.get("aria-label", "") or tag.get("aria-labelledby", ""),
            "autocomplete": tag.get("autocomplete", "") or "",
            "onclick": tag.get("onclick", "") or "",
            "data_attributes": {k: v for k, v in tag.attrs.items() if k.startswith("data-")},
            "css_selectors": ([f"#{tag.get('id')}"] if tag.get("id") else []) +
                             ([f"input[name='{tag.get('name')}']"] if tag.get("name") else []) +
                             ([f"input[placeholder='{tag.get('placeholder')}']"] if tag.get(
                                 "placeholder") else []),
            "text": tag.get_text(strip=True) or ""
        })

    # TEXTAREAS
    for tag in soup.find_all("textarea"):
        fields.append({
            "element_type": "textarea",
            "tag": "textarea",
            "type": "textarea",
            "name": tag.get("name", "") or "",
            "id": tag.get("id", "") or "",
            "placeholder": tag.get("placeholder", "") or "",
            "class": cls(tag),
            "value": tag.get_text(strip=True),
            "required": tag.has_attr("required"),
            "disabled": tag.has_attr("disabled"),
            "displayed": True,
            "rows": tag.get("rows", "") or "",
            "cols": tag.get("cols", "") or "",
            "aria_label": tag.get("aria-label", "") or tag.get("aria-labelledby", ""),
            "autocomplete": tag.get("autocomplete", "") or "",
            "onclick": tag.get("onclick", "") or "",
            "data_attributes": {k: v for k, v in tag.attrs.items() if k.startswith("data-")},
            "css_selectors": ([f"#{tag.get('id')}"] if tag.get("id") else []) +
                             ([f"textarea[name='{tag.get('name')}']"] if tag.get(
                                 "name") else []),
            "text": tag.get_text(strip=True) or ""
        })

    # SELECTS
    for tag in soup.find_all("select"):
        options = [{"value": o.get("value", ""), "text": o.get_text(strip=True), "selected": o.has_attr("selected")}
                   for o in tag.find_all("option")]
        fields.append({
            "element_type": "select",
            "tag": "select",
            "type": "select",
            "name": tag.get("name", "") or "",
            "id": tag.get("id", "") or "",
            "class": cls(tag),
            "required": tag.has_attr("required"),
            "disabled": tag.has_attr("disabled"),
            "displayed": True,
            "multiple": tag.has_attr("multiple"),
            "options": options,
            "options_count": len(options),
            "aria_label": tag.get("aria-label", "") or tag.get("aria-labelledby", ""),
            "autocomplete": tag.get("autocomplete", "") or "",
            "onclick": tag.get("onclick", "") or "",
            "data_attributes": {k: v for k, v in tag.attrs.items() if k.startswith("data-")},
            "css_selectors": ([f"#{tag.get('id')}"] if tag.get("id") else []) +
                             ([f"select[name='{tag.get('name')}']"] if tag.get(
                                 "name") else []),
            "text": tag.get_text(strip=True) or ""
        })

    # BUTTONS
    for tag in soup.find_all("button"):
        fields.append({
            "element_type": "button",
            "tag": "button",
            "type": f"button_{tag.get('type', 'button')}",
            "name": tag.get("name", "") or "",
            "id": tag.get("id", "") or "",
            "class": cls(tag),
            "text": tag.get_text(strip=True) or "",
            "value": tag.get("value", "") or "",
            "disabled": tag.has_attr("disabled"),
            "displayed": True,
            "onclick": tag.get("onclick", "") or "",
            "form": tag.get("form", "") or "",
            "aria_label": tag.get("aria-label", "") or tag.get("aria-labelledby", ""),
            "data_attributes": {k: v for k, v in tag.attrs.items() if k.startswith("data-")},
            "css_selectors": ([f"#{tag.get('id')}"] if tag.get("id") else []) +
                             ([f"button:contains('{tag.get_text(strip=True)}')"] if tag.get_text(
                                 strip=True) else [])
        })

    # INPUT buttons (submit/reset)
    for tag in soup.find_all("input", {"type": ["button", "submit", "reset"]}):
        fields.append({
            "element_type": "button",
            "tag": "input",
            "type": tag.get("type", "button"),
            "name": tag.get("name", "") or "",
            "id": tag.get("id", "") or "",
            "class": cls(tag),
            "text": tag.get("value", "") or "",
            "value": tag.get("value", "") or "",
            "disabled": tag.has_attr("disabled"),
            "displayed": True,
            "onclick": tag.get("onclick", "") or "",
            "form": tag.get("form", "") or "",
            "data_attributes": {k: v for k, v in tag.attrs.items() if k.startswith("data-")},
            "css_selectors": ([f"#{tag.get('id')}"] if tag.get("id") else []) +
                             ([f"input[value='{tag.get('value', '')}']"] if tag.get(
                                 "value") else [])
        })

    # LINKS
    for tag in soup.find_all("a", href=True):
        fields.append({
            "element_type": "link",
            "tag": "a",
            "href": tag.get("href", "") or "",
            "id": tag.get("id", "") or "",
            "class": cls(tag),
            "text": tag.get_text(strip=True) or "",
            "target": tag.get("target", "") or "",
            "rel": tag.get("rel", "") or "",
            "onclick": tag.get("onclick", "") or "",
            "data_attributes": {k: v for k, v in tag.attrs.items() if k.startswith("data-")}
        })

    # DIV / SPAN with meaningful text
    for tag in soup.find_all("div"):
        t = tag.get_text(" ", strip=True)
        if t:
            fields.append({"element_type": "div", "tag": "div", "id": tag.get(
                "id", ""), "class": cls(tag), "text": t[:1000]})
    for tag in soup.find_all("span"):
        t = tag.get_text(" ", strip=True)
        if t:
            fields.append({"element_type": "span", "tag": "span", "id": tag.get(
                "id", ""), "class": cls(tag), "text": t[:500]})

    # LISTS
    for ul in soup.find_all("ul"):
        items = [li.get_text(strip=True) for li in ul.find_all("li")]
        if items:
            fields.append({"element_type": "ul", "tag": "ul", "id": ul.get(
                "id", ""), "class": cls(ul), "items": items})
    for ol in soup.find_all("ol"):
        items = [li.get_text(strip=True) for li in ol.find_all("li")]
        if items:
            fields.append({"element_type": "ol", "tag": "ol", "id": ol.get(
                "id", ""), "class": cls(ol), "items": items})

    # FORMS (with nested controls)
    forms = []
    for idx, form in enumerate(soup.find_all("form")):
        controls = []
        for c in form.find_all(["input", "textarea", "select", "button"]):
            ctrl = {
                "name": c.get("name", "") or "",
                "type": (c.get("type", "") if c.name == "input" else ("textarea" if c.name == "textarea" else ("select" if c.name == "select" else f"button_{c.get('type', 'button')}"))),
                "id": c.get("id", "") or "",
                "placeholder": c.get("placeholder", "") or "",
                "class": cls(c),
                "required": c.has_attr("required"),
                "disabled": c.has_attr("disabled"),
                "value": (c.get("value", "") or c.get_text(strip=True) if c.name in ("input", "textarea") else ""),
                "text": (c.get_text(strip=True) if c.name == "button" else ""),
                "autocomplete": c.get("autocomplete", "") or "",
                "aria_label": c.get("aria-label", "") or c.get("aria-labelledby", ""),
                "onclick": c.get("onclick", "") or ""
            }
            if c.name == "select":
                ctrl["options"] = [opt.get_text(
                    strip=True) for opt in c.find_all("option")]
                ctrl["multiple"] = c.has_attr("multiple")
            controls.append(ctrl)

        forms.append({
            "form_index": idx,
            "action": form.get("action", "") or "",
            "method": (form.get("method", "get") or "get").upper(),
            "name": form.get("name", "") or "",
            "id": form.get("id", "") or "",
            "class": cls(form),
            "enctype": form.get("enctype", "application/x-www-form-urlencoded"),
            "target": form.get("target", "") or "",
            "autocomplete": form.get("autocomplete", "") or "",
            "novalidate": form.has_attr("novalidate"),
            "fields": controls,
            "field_count": len(controls)
        })

    # Title, headings
    title = (soup.title.string.strip()
             if soup.title and soup.title.string else "")
    headings = []
    for i in range(1, 7):
        for h in soup.find_all(f"h{i}"):
            headings.append({"level": i, "text": h.get_text(
                strip=True), "id": h.get("id", ""), "class": cls(h)})

    # Field summary (used by UI + DB)
    input_types = {}
    for f in fields:
        if f.get("element_type") == "input":
            t = (f.get("type") or "text").lower()
            input_types[t] = input_types.get(t, 0) + 1

    field_summary = {
        "total_fields": len(fields),
        "named_fields": sum(1 for f in fields if f.get("name")),
        "id_fields": sum(1 for f in fields if f.get("id")),
        "placeholder_fields": sum(1 for f in fields if f.get("placeholder")),
        "required_fields": sum(1 for f in fields if f.get("required")),
        "aria_labeled_fields": sum(1 for f in fields if f.get("aria_label")),
        "class_fields": sum(1 for f in fields if f.get("class")),
        "data_attribute_fields": sum(1 for f in fields if f.get("data_attributes")),
        "input_types": input_types,
        "forms_count": len(forms),
        "total_actions": sum(1 for f in fields if f.get("element_type") == "button")
    }

    return {
        "title": title,
        "fields": fields,
        "forms": forms,
        "headings": headings,
        "field_summary": field_summary,
        "timestamp": int(time.time() * 1000),
        "url_analyzed": True
    }

def extract_enhanced_metadata_with_selenium(html, browser):
    """
    Base = BeautifulSoup parse; then augment with Selenium + Shadow DOM.
    """
    base = extract_enhanced_metadata(html)

    # Selenium augment: dynamic fields + forms
    dyn_fields = extract_dynamic_fields_selenium(browser) or []
    sel_forms  = extract_forms_with_selenium(browser) or []
    shadow     = extract_shadow_dom_fields(browser) or []

    # Merge fields (avoid obvious duplicates by (name,id,type,placeholder))
    def sig(f):
        return (
            f.get("name",""), f.get("id",""),
            f.get("type",""), f.get("placeholder",""),
            f.get("element_type", f.get("tag",""))
        )
    seen = {sig(f) for f in base.get("fields", [])}
    for f in dyn_fields + shadow:
        ff = dict(f)
        ff.setdefault("element_type", ff.get("tag", "input"))
        ff.setdefault("source", "selenium")
        if sig(ff) not in seen:
            base["fields"].append(ff)
            seen.add(sig(ff))

    # Prefer Selenium-discovered forms if we found any
    if sel_forms:
        base["forms"] = sel_forms

    # Refresh summary counts
    fs = base.get("field_summary", {})
    fields = base.get("fields", [])
    input_types = {}
    for f in fields:
        if (f.get("element_type") == "input") or (f.get("tag") == "input"):
            t = (f.get("type") or "text").lower()
            input_types[t] = input_types.get(t, 0) + 1

    fs.update({
        "total_fields": len(fields),
        "named_fields": sum(1 for f in fields if f.get("name")),
        "id_fields":    sum(1 for f in fields if f.get("id")),
        "placeholder_fields": sum(1 for f in fields if f.get("placeholder")),
        "required_fields":    sum(1 for f in fields if f.get("required")),
        "aria_labeled_fields":sum(1 for f in fields if f.get("aria_label")),
        "class_fields":       sum(1 for f in fields if f.get("class")),
        "data_attribute_fields": sum(1 for f in fields if f.get("data_attributes")),
        "input_types": input_types,
        "forms_count": len(base.get("forms", [])),
        "selenium_fields": len(dyn_fields),
        "shadow_dom_fields": len(shadow),
        "enhancement_level": "enhanced",
    })
    base["field_summary"] = fs
    base["enhanced_with_selenium"] = True
    return base


def record_action(url, content, referrer=None):
    """
    Enhanced record action with comprehensive field detection and proper storage
    """
    global field_stats, browser

    print(f"\n🔍 Processing page: {url}")

    # Use enhanced metadata extraction with Selenium support if browser is alive
    if browser and is_browser_alive():
        try:
            print("🤖 Using enhanced Selenium detection...")
            # Debug page structure for dev visibility
            debug_page_structure(browser)

            # Extract metadata using BeautifulSoup + Selenium + Shadow DOM
            metadata = extract_enhanced_metadata_with_selenium(
                content, browser)
            print(f"✅ Enhanced extraction completed")
        except Exception as e:
            print(f"⚠️ Selenium enhancement failed, falling back: {e}")
            metadata = extract_enhanced_metadata(content)
    else:
        print("⚠️ Browser not available, using BeautifulSoup only")
        metadata = extract_enhanced_metadata(content)

    # Store or update in-memory flows
    previous_actions = flows.get(url, {}).get("metadata", {}).get("page_actions")
    if previous_actions:
        metadata["page_actions"] = previous_actions
    G.add_node(url, metadata=metadata)
    flows[url] = {"metadata": metadata, "content": content}
    if referrer and referrer in flows:
        G.add_edge(referrer, url)

    # Update global field stats
    field_summary = metadata.get("field_summary", {})
    field_stats[url] = field_summary

    # 📊 Debug output with detailed logging
    print("📊 Field Detection Results:")
    print(f"  • Total fields: {field_summary.get('total_fields', 0)}")
    print(f"  • Named fields: {field_summary.get('named_fields', 0)}")
    print(f"  • ID fields: {field_summary.get('id_fields', 0)}")
    print(
        f"  • Placeholder fields: {field_summary.get('placeholder_fields', 0)}")
    print(f"  • Required fields: {field_summary.get('required_fields', 0)}")
    print(f"  • Forms found: {field_summary.get('forms_count', 0)}")
    print(
        f"  • Enhanced with Selenium: {metadata.get('enhanced_with_selenium', False)}")

    # 🔍 Sample fields with more detail
    fields = metadata.get("fields", [])
    if fields:
        print("🔍 Sample Fields Found:")
        for i, field in enumerate(fields[:5]):
            field_name = field.get('name') or field.get(
                'id') or field.get('placeholder') or f"field_{i}"
            field_type = field.get('type', 'unknown')
            source = field.get('source', 'beautifulsoup')
            selectors = field.get('css_selectors', [])
            element_type = field.get('element_type', 'unknown')
            print(f"  Field {i+1}: {element_type}/{field_type} - Name:'{field.get('name', 'NO_NAME')}' ID:'{field.get('id', 'NO_ID')}' Placeholder:'{field.get('placeholder', 'NO_PLACEHOLDER')}' (via {source})")
            if selectors and len(selectors) > 0:
                print(
                    f"    CSS Selectors: {', '.join(str(s) for s in selectors[:2])}")
    else:
        print("⚠️ No fields detected - this might indicate a problem")

    # 📝 Forms with detailed field breakdown
    forms = metadata.get("forms", [])
    if forms:
        print("📝 Forms Found:")
        for i, form in enumerate(forms):
            form_name = form.get('name') or form.get('id') or f"form_{i}"
            action = form.get('action', 'No action')
            method = form.get('method', 'GET')
            field_count = form.get('field_count', 0)
            print(
                f"  Form {i+1}: '{form_name}' - {method} to '{action}' ({field_count} fields)")

            form_fields = form.get('fields', [])
            if form_fields:
                print("    Form Fields:")
                # Show first 3 fields
                for j, field in enumerate(form_fields[:3]):
                    field_name = field.get('name', 'NO_NAME')
                    field_id = field.get('id', 'NO_ID')
                    field_type = field.get('type', 'unknown')
                    placeholder = field.get('placeholder', 'NO_PLACEHOLDER')
                    print(
                        f"      • {field_type}: Name='{field_name}' ID='{field_id}' Placeholder='{placeholder}'")

                    # Show data attributes if present
                    data_attrs = field.get('data_attributes', {})
                    if data_attrs:
                        print(
                            f"        Data attributes: {list(data_attrs.keys())}")

                if len(form_fields) > 3:
                    print(f"      ... and {len(form_fields) - 3} more fields")
    else:
        print("⚠️ No forms detected on this page")

    # Get current session ID
    session_id = None
    try:
        from database.history_manager import history_manager
        if history_manager.current_session:
            session_id = history_manager.current_session.id
    except Exception as e:
        print(f"⚠️ Could not get session ID: {e}")

    # Store in databases with explicit session ID
    print(f"💾 Storing in databases...")
    neo4j_success = store_in_neo4j_enhanced(
        url, metadata, referrer, session_id)
    vector_success = store_in_pgvector_enhanced(
        url, content, metadata)

    print(
        f"💾 Storage results: Neo4j={'✅' if neo4j_success else '❌'}, Vector={'✅' if vector_success else '❌'}")

    # Update UI signals
    try:
        signals.field_stats_updated.emit(field_summary)
    except Exception as e:
        print(f"⚠️ Could not emit UI signal: {e}")

    return True

def store_in_neo4j_enhanced(url, metadata, referrer=None, session_id=None):
    try:
        return store_in_neo4j_core(url, metadata, referrer, session_id)
    except Exception as e:
        print(f"❌ Error storing in Neo4j: {e}")
        return False


def store_in_pgvector_enhanced(url, content, metadata, session_id=None):
    try:
        return store_in_pgvector_core(url, content, metadata, session_id)
    except Exception as e:
        print(f"❌ Error storing in PGVector: {e}")
        return False


def store_page_actions(url, page_actions):
    try:
        update_page_actions_core(url, page_actions)            
        append_page_actions_core(url, page_actions)            
    except Exception as e:
        print(f"❌ Error mirroring page_actions: {e}")


def is_browser_alive():
    global browser
    try:
        if browser is None:
            return False
        browser.current_window_handle
        return True
    except:
        return False



def start_browser():
    global browser

    if is_browser_alive():
        return True

    try:
        chrome_options = Options()
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_experimental_option(
            "excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_experimental_option("detach", True)

        browser = webdriver.Chrome(options=chrome_options)
        browser.get("about:blank")

        return True
    except Exception as e:
        print(f"Error starting browser: {e}")
        signals.error.emit("Browser Error", f"Failed to start browser: {e}")
        return False


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

# Enhanced capture function

def _parse_iso_ts(ts: str) -> float:
    try:
        return datetime.datetime.fromisoformat(
            ts.replace("Z", "+00:00")
        ).timestamp() * 1000.0
    except Exception:
        return 0.0

def _dedupe_and_compress_actions(url: str, existing_actions: list, new_actions: list, window_ms: int = 700):
    """
    Returns (to_append, updated_indices)

    - Drops rapid duplicate clicks of same (type, field_xpath, form_xpath) in window_ms
    - For 'enter': if a prior action exists for same field_xpath, we UPDATE that prior
      action in-place (data/timestamp/flags) instead of appending a new row.
    """
    to_append = []
    updated_indices = set()

    # quick lookups
    def ts_ms(a):
        try:
            return datetime.datetime.fromisoformat(a.get("timestamp","").replace("Z","+00:00")).timestamp()*1000
        except Exception:
            return 0.0

    recent = {}  
    for i, a in enumerate(existing_actions[-10:]):
        key = (a.get("type"), a.get("field_xpath",""), a.get("form_xpath",""))
        recent[key] = ts_ms(a)

    # map last 'enter' by field across ALL existing
    last_enter_idx_by_field = {}
    for idx in range(len(existing_actions)-1, -1, -1):
        a = existing_actions[idx]
        if a.get("type") == "enter":
            fx = a.get("field_xpath","")
            if fx and fx not in last_enter_idx_by_field:
                last_enter_idx_by_field[fx] = idx

    for a in new_actions:
        t  = a.get("type")
        fx = a.get("field_xpath","")
        fpx= a.get("form_xpath","")
        key= (t, fx, fpx)
        cur= ts_ms(a)

        # debounce fast duplicates
        last = recent.get(key, -1e18)
        if cur - last < window_ms:
            continue
        recent[key] = cur

        if t == "enter" and fx:
            # update prior 'enter' for same field if present
            if fx in last_enter_idx_by_field:
                idx = last_enter_idx_by_field[fx]
                prev = existing_actions[idx]
                prev["data"]       = a.get("data")
                prev["sensitive"]  = bool(a.get("sensitive", prev.get("sensitive", False)))
                prev["field_label"]= prev.get("field_label") or a.get("field_label","")
                prev["form_name"]  = prev.get("form_name") or a.get("form_name","")
                prev["form_xpath"] = prev.get("form_xpath") or a.get("form_xpath","")
                prev["timestamp"]  = a.get("timestamp")
                updated_indices.add(idx)
                # keep the mapping pointing to this idx (latest)
                last_enter_idx_by_field[fx] = idx
                continue  # do NOT append
            else:
                # first time we see this field: append; remember its index
                last_enter_idx_by_field[fx] = len(existing_actions) + len(to_append)

        to_append.append(a)

    return to_append, updated_indices


def capture_web_actions():
    """
    Enhanced web actions capture with comprehensive field detection
    """
    global browser, stop_capturing, TARGET_WEBSITE

    if not TARGET_WEBSITE.startswith(('http://', 'https://')):
        TARGET_WEBSITE = 'https://' + TARGET_WEBSITE

    try:
        if browser is None or not is_browser_alive():
            signals.error.emit(
                "Browser Error", "Browser is not running. Please start the browser first.")
            return

        print(f"🌐 Opening target website: {TARGET_WEBSITE}")
        signals.update_status.emit(
            f"🌐 Opening target website: {TARGET_WEBSITE}")

        # Navigate to target website
        browser.get(TARGET_WEBSITE)
        _install_action_listeners(browser)

        # Wait for page to fully load
        try:
            WebDriverWait(browser, 10).until(
                lambda driver: driver.execute_script(
                    "return document.readyState") == "complete")
            _install_action_listeners(browser)
        

            print("✅ Page loaded completely")
        except TimeoutException:
            print("⚠️ Page load timeout, proceeding anyway")

        # Get initial page data
        url = browser.current_url
        html_content = browser.page_source

        print(f"📄 Processing initial page: {url}")
        print(f"📄 HTML content length: {len(html_content)} characters")

        # Record initial page with enhanced processing
        record_action(url, html_content)

        # Initial analysis report
        metadata = flows[url]["metadata"]
        field_summary = metadata.get("field_summary", {})

        initial_report = (
            f"📊 Initial Page Analysis Complete:\n"
            f"  🔢 Total Fields: {field_summary.get('total_fields', 0)}\n"
            f"  🏷️ Named Fields: {field_summary.get('named_fields', 0)}\n"
            f"  🆔 ID Fields: {field_summary.get('id_fields', 0)}\n"
            f"  💬 Placeholder Fields: {field_summary.get('placeholder_fields', 0)}\n"
            f"  ⚠️ Required Fields: {field_summary.get('required_fields', 0)}\n"
            f"  📝 Forms Found: {field_summary.get('forms_count', 0)}\n"
            f"  🖱️ Interactive Elements: {field_summary.get('total_actions', 0)}\n"
            f"  🔧 Selenium Enhanced: {metadata.get('enhanced_with_selenium', False)}"
        )

        signals.update_status.emit(initial_report)
        print(initial_report)

        # Show field breakdown by source if enhanced with Selenium
        if metadata.get('enhanced_with_selenium'):
            selenium_fields = field_summary.get('selenium_fields', 0)
            shadow_fields = field_summary.get('shadow_dom_fields', 0)
            print(f"  🔍 Enhanced Detection:")
            print(f"    • Selenium fields: {selenium_fields}")
            print(f"    • Shadow DOM fields: {shadow_fields}")

        last_url = url
        last_content_hash = hash(html_content)
        page_check_count = 0

        print(f"🔄 Starting continuous monitoring...")
        signals.update_status.emit(
            "🔄 Monitoring for page changes and form interactions...")

        # Continuously monitor for page changes
        while not stop_capturing:
            if not is_browser_alive():
                signals.error.emit(
                    "Browser Error", "Browser window was closed")
                break

            try:
                current_url = browser.current_url
                page_check_count += 1

                # Every 10 checks, print a status update
                if page_check_count % 20 == 0:
                    print(f"🔍 Still monitoring... (check #{page_check_count})")

                # If URL has changed, record the new page
                if current_url != last_url:
                    print(f"\n🔄 URL changed: {last_url} → {current_url}")
                    signals.update_status.emit(
                        f"🔄 New page detected: {current_url}")

                    # Wait a moment for new page to load
                    time.sleep(1)

                    try:
                        WebDriverWait(browser, 5).until(
                            lambda driver: driver.execute_script(
                                "return document.readyState") == "complete")

                    except TimeoutException:
                        print("⚠️ New page load timeout, proceeding anyway")

                    html_content = browser.page_source
                    record_action(current_url, html_content, last_url)

                    # Enhanced logging with comprehensive field information
                    metadata = flows[current_url]["metadata"]
                    field_summary = metadata.get("field_summary", {})

                    analysis_update = (
                        f"✅ New page captured: {current_url}\n"
                        f"  🔢 Fields: {field_summary.get('total_fields', 0)} total "
                        f"({field_summary.get('named_fields', 0)} named, "
                        f"{field_summary.get('id_fields', 0)} with ID)\n"
                        f"  📝 Forms: {field_summary.get('forms_count', 0)}\n"
                        f"  🖱️ Actions: {field_summary.get('total_actions', 0)}"
                    )

                    signals.update_status.emit(analysis_update)
                    print(analysis_update)

                    last_url = current_url
                    _install_action_listeners(browser)
                    last_content_hash = hash(html_content)

                # Check for content changes on the same page (AJAX, modal opens, form changes)
                elif current_url == last_url:
                    html_content = browser.page_source
                    current_content_hash = hash(html_content)

                    # Content changed (could be modal opening, form appearing, AJAX update)
                    if current_content_hash != last_content_hash:
                        print(
                            f"\n🔄 Content changed on same page: {current_url}")
                        signals.update_status.emit(
                            f"🔄 Content updated: {current_url}")

                        record_action(current_url, html_content)

                        metadata = flows[current_url]["metadata"]
                        field_summary = metadata.get("field_summary", {})

                        update_info = (
                            f"✅ Content updated: {current_url}\n"
                            f"  🔢 Fields: {field_summary.get('total_fields', 0)}\n"
                            f"  📝 Forms: {field_summary.get('forms_count', 0)}"
                        )

                        signals.update_status.emit(update_info)
                        print(update_info)

                        last_content_hash = current_content_hash        
                try:
                    actions = _drain_actions(browser)
                    if actions:
                        url_for_actions = browser.current_url

                        # assign incremental action_id after filtering
                        page_actions_obj = flows.setdefault(url_for_actions, {}).setdefault("metadata", {}).setdefault("page_actions", {"actions": []})
                        existing = page_actions_obj["actions"]

                        # filter & compress
                        to_append, updated_ix = _dedupe_and_compress_actions(url_for_actions, existing, actions)

                        # apply new action_ids to things we will append
                        if to_append:
                            next_id = action_counters.get(url_for_actions, 1)
                            for a in to_append:
                                a["action_id"] = next_id
                                next_id += 1
                            action_counters[url_for_actions] = next_id
                            existing.extend(to_append)

                        # persist:
                        # - if we only updated existing rows -> call UPDATE mirror only
                        # - if we appended -> call both append (for the new ones) and update (to mirror full state)
                        if to_append:
                            update_page_actions_core(url_for_actions, page_actions_obj)  # mirror full state (Neo4j)
                            append_page_actions_core(url_for_actions, {"actions": to_append})  # append only the new rows (Postgres)
                        elif updated_ix:
                            # no new rows; just mirror the updated JSON structure to both stores
                            update_page_actions_core(url_for_actions, page_actions_obj)

                except Exception as e:
                    print(f"Action drain error: {e}")

                # Brief pause to avoid high CPU usage
                time.sleep(0.5)

            except WebDriverException as e:
                print(
                    f"WebDriver error in capture loop (page might be navigating): {e}")
                time.sleep(2)  # Give browser more time to settle
                if not is_browser_alive():
                    break
            except Exception as e:
                print(f"Unexpected error in capture loop: {e}")
                time.sleep(1)
                if not is_browser_alive():
                    break

    except Exception as e:
        error_msg = f"Error in enhanced capture thread: {e}"
        print(error_msg)
        signals.error.emit("Capture Error", error_msg)

    print("🛑 Enhanced capture thread stopping...")
    signals.update_status.emit("🛑 Enhanced capture thread stopped")

# Enhanced export function

def export_enhanced_visualization():
    """
    Export comprehensive data including field analysis and form structures
    """
    try:
        # Create a comprehensive NetworkX graph visualization
        plt.figure(figsize=(15, 12))
        pos = nx.spring_layout(G, k=1, iterations=50)

        # Color nodes based on field count
        node_colors = []
        node_sizes = []
        for node in G.nodes():
            field_count = field_stats.get(node, {}).get('total_fields', 0)
            if field_count == 0:
                node_colors.append('lightgray')
                node_sizes.append(800)
            elif field_count < 5:
                node_colors.append('lightblue')
                node_sizes.append(1200)
            elif field_count < 10:
                node_colors.append('orange')
                node_sizes.append(1600)
            else:
                node_colors.append('red')
                node_sizes.append(2000)

        nx.draw(G, pos, with_labels=False, node_color=node_colors,
                node_size=node_sizes, edge_color='gray', arrows=True,
                arrowsize=20, alpha=0.8)

        # Add labels with field counts
        labels = {}
        for node in G.nodes():
            field_count = field_stats.get(node, {}).get('total_fields', 0)
            domain = urlparse(node).path.split('/')[-1] or 'home'
            labels[node] = f"{domain}\n({field_count} fields)"

        nx.draw_networkx_labels(G, pos, labels, font_size=8)

        plt.title("Enhanced Web Flow Capture - Field Analysis", fontsize=16)
        plt.figtext(0.02, 0.02, "Node size and color indicate field count: Gray=0, Blue=1-4, Orange=5-9, Red=10+",
                    fontsize=10)
        plt.tight_layout()
        plt.savefig("enhanced_web_flow_capture.png",
                    dpi=300, bbox_inches='tight')
        plt.close()

        # Create comprehensive report
        report = "Enhanced Web Flow Capture Report\n"
        report += "=" * 80 + "\n\n"
        report += f"Target Website: {TARGET_WEBSITE}\n"
        report += f"Total Pages Captured: {len(flows)}\n"
        report += f"Total Unique Forms Found: {sum(stats.get('forms_count', 0) for stats in field_stats.values())}\n"
        report += f"Total Fields Detected: {sum(stats.get('total_fields', 0) for stats in field_stats.values())}\n"
        report += f"Capture Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        # Add summary statistics
        report += "FIELD DETECTION SUMMARY\n"
        report += "-" * 40 + "\n"
        total_pages_with_fields = len(
            [stats for stats in field_stats.values() if stats.get('total_fields', 0) > 0])
        total_pages_with_forms = len(
            [stats for stats in field_stats.values() if stats.get('forms_count', 0) > 0])

        report += f"Pages with fields: {total_pages_with_fields}/{len(flows)}\n"
        report += f"Pages with forms: {total_pages_with_forms}/{len(flows)}\n"
        report += f"Average fields per page: {sum(stats.get('total_fields', 0) for stats in field_stats.values()) / max(len(flows), 1):.1f}\n\n"

        # Detailed page analysis
        report += "DETAILED PAGE ANALYSIS\n"
        report += "=" * 80 + "\n\n"

        for url, data in flows.items():
            metadata = data['metadata']
            field_summary = metadata.get('field_summary', {})

            report += f"URL: {url}\n"
            report += f"Title: {metadata.get('title', 'No title')}\n"
            report += f"Timestamp: {datetime.fromtimestamp(metadata.get('timestamp', 0)/1000).strftime('%Y-%m-%d %H:%M:%S')}\n"

            # Field statistics
            report += f"Field Statistics:\n"
            report += f"  - Total fields: {field_summary.get('total_fields', 0)}\n"
            report += f"  - Named fields: {field_summary.get('named_fields', 0)}\n"
            report += f"  - ID fields: {field_summary.get('id_fields', 0)}\n"
            report += f"  - Placeholder fields: {field_summary.get('placeholder_fields', 0)}\n"
            report += f"  - Required fields: {field_summary.get('required_fields', 0)}\n"
            report += f"  - ARIA labeled fields: {field_summary.get('aria_labeled_fields', 0)}\n"

            # Input types breakdown
            input_types = field_summary.get('input_types', {})
            if input_types:
                report += f"  - Input types: {', '.join([f'{k}({v})' for k,
                                                        v in input_types.items()])}\n"

            # Forms analysis
            forms = metadata.get('forms', [])
            report += f"Forms found: {len(forms)}\n"
            for i, form in enumerate(forms):
                report += f"  Form {i+1}:\n"
                report += f"    - Action: {form.get('action', 'No action')}\n"
                report += f"    - Method: {form.get('method', 'GET')}\n"
                report += f"    - Fields: {form.get('field_count', 0)}\n"
                report += f"    - ID: {form.get('id', 'No ID')}\n"
                report += f"    - Class: {form.get('class', 'No class')}\n"

                # Sample field details
                form_fields = form.get('fields', [])[:5] 
                if form_fields:
                    report += f"    - Sample fields:\n"
                    for j, field in enumerate(form_fields):
                        field_id = field.get('name') or field.get(
                            'id') or field.get('placeholder') or f'field_{j}'
                        report += f"      • {field.get('type', 'text')}: {field_id}\n"

            # Actions found
            actions = metadata.get('actions', [])
            if actions:
                report += f"Actions found: {len(actions)}\n"
                for action in actions[:5]:  
                    action_id = action.get('text') or action.get(
                        'id') or action.get('onclick', 'Unknown action')
                    report += f"  - {action.get('type', 'unknown')}: {action_id}\n"

            report += "\n" + "-" * 80 + "\n\n"

        # CSS Selectors for automation
        report += "CSS SELECTORS FOR TEST AUTOMATION\n"
        report += "=" * 80 + "\n\n"

        for url, data in flows.items():
            metadata = data['metadata']
            fields = metadata.get('fields', [])

            if fields:
                report += f"Page: {url}\n"
                report += "Field Selectors:\n"

                for i, field in enumerate(fields):
                    selectors = field.get('css_selectors', [])
                    if selectors:
                        field_type = field.get('type', 'unknown')
                        field_name = field.get('name') or field.get(
                            'id') or f'field_{i}'
                        report += f"  {field_name} ({field_type}):\n"
                        for selector in selectors:
                            report += f"    - {selector}\n"

                # Button/action selectors
                actions = metadata.get('actions', [])
                if actions:
                    report += "Action Selectors:\n"
                    for action in actions:
                        selectors = action.get('css_selectors', [])
                        if selectors:
                            action_name = action.get(
                                'text') or action.get('id') or 'unknown'
                            report += f"  {action_name} ({action.get('type', 'unknown')}):\n"
                            for selector in selectors:
                                report += f"    - {selector}\n"

                report += "\n"

        # Write comprehensive report to file
        with open("enhanced_web_flow_report.txt", "w", encoding='utf-8') as f:
            f.write(report)

        # Export field data as JSON for programmatic use
        export_data = {
            "metadata": {
                "target_website": TARGET_WEBSITE,
                "capture_time": datetime.now().isoformat(),
                "total_pages": len(flows),
                "total_fields": sum(stats.get('total_fields', 0) for stats in field_stats.values()),
                "total_forms": sum(stats.get('forms_count', 0) for stats in field_stats.values())
            },
            "pages": []
        }

        for url, data in flows.items():
            page_data = {
                "url": url,
                "metadata": data['metadata'],
                "field_stats": field_stats.get(url, {})
            }
            export_data["pages"].append(page_data)

        with open("enhanced_field_data.json", "w", encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)

        return True
    except Exception as e:
        print(f"Error exporting enhanced visualization: {e}")
        return False

# Enhanced PyQt5 UI Class
class WebFlowCaptureApp(QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.connectSignals()
        self.is_capturing = False

    def initUI(self):
        self.setWindowTitle("Enhanced Web Flow Capture - Field Analysis")
        self.setGeometry(100, 100, 800, 700)

        # Create main layout
        main_layout = QVBoxLayout()

        # Header
        header_label = QLabel("Enhanced Web Flow Capture")
        header_label.setFont(QFont("Arial", 16, QFont.Bold))
        header_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(header_label)

        # Target Website Input
        website_layout = QHBoxLayout()
        self.website_label = QLabel("Target Website:")
        self.website_entry = QLineEdit()
        self.website_entry.setPlaceholderText("https://example.com")
        self.website_entry.textChanged.connect(self.validate_inputs)
        website_layout.addWidget(self.website_label)
        website_layout.addWidget(self.website_entry)
        main_layout.addLayout(website_layout)

        # Browser Control Buttons
        browser_buttons_layout = QHBoxLayout()

        self.start_browser_button = QPushButton("🚀 Start Browser")
        self.start_browser_button.clicked.connect(self.start_browser_clicked)
        browser_buttons_layout.addWidget(self.start_browser_button)

        self.stop_browser_button = QPushButton("🛑 Stop Browser")
        self.stop_browser_button.clicked.connect(self.stop_browser_clicked)
        self.stop_browser_button.setEnabled(False)
        browser_buttons_layout.addWidget(self.stop_browser_button)

        main_layout.addLayout(browser_buttons_layout)

        # Capture Control Buttons
        capture_buttons_layout = QHBoxLayout()

        self.start_capture_button = QPushButton("🎯 Start Capturing")
        self.start_capture_button.setEnabled(False)
        self.start_capture_button.clicked.connect(self.start_capturing)
        capture_buttons_layout.addWidget(self.start_capture_button)

        self.stop_capture_button = QPushButton("⏹ Stop Capturing")
        self.stop_capture_button.setEnabled(False)
        self.stop_capture_button.clicked.connect(self.stop_capturing)
        capture_buttons_layout.addWidget(self.stop_capture_button)

        self.export_button = QPushButton("📊 Export Enhanced Data")
        self.export_button.clicked.connect(self.export_data)
        capture_buttons_layout.addWidget(self.export_button)

        main_layout.addLayout(capture_buttons_layout)

        # Field Statistics Panel
        stats_label = QLabel("Field Detection Statistics")
        stats_label.setFont(QFont("Arial", 12, QFont.Bold))
        main_layout.addWidget(stats_label)

        self.stats_display = QLabel("No data captured yet")
        self.stats_display.setStyleSheet("""
            background-color: #f8f9fa; 
            border: 1px solid #dee2e6; 
            padding: 10px; 
            border-radius: 5px;
            font-family: monospace;
        """)
        self.stats_display.setWordWrap(True)
        main_layout.addWidget(self.stats_display)

        # Instructions
        instructions_label = QLabel("Enhanced Capture Instructions")
        instructions_label.setFont(QFont("Arial", 10, QFont.Bold))
        main_layout.addWidget(instructions_label)

        self.instructions_label = QLabel(
            "🎯 Enhanced Features:\n"
            "• Captures fields using multiple identification methods (name, id, placeholder, aria-label)\n"
            "• Real-time field statistics and form analysis\n"
            "• CSS selector generation for test automation\n"
            "• Comprehensive export with JSON data\n\n"
            "📋 Steps:\n"
            "1. Enter target website URL\n"
            "2. Click 'Start Browser' to launch controlled Chrome\n"
            "3. Click 'Start Capturing' to begin field analysis\n"
            "4. Navigate and interact with forms on the website\n"
            "5. Watch real-time field statistics below\n"
            "6. Click 'Export Enhanced Data' for comprehensive reports"
        )
        self.instructions_label.setStyleSheet(
            "background-color: #e3f2fd; padding: 10px; border-radius: 5px;")
        self.instructions_label.setWordWrap(True)
        main_layout.addWidget(self.instructions_label)

        # Status Label
        self.status_label = QLabel("Status: Ready")
        self.status_label.setFont(QFont("Arial", 10, QFont.Bold))
        main_layout.addWidget(self.status_label)

        # Capture Log with scroll area
        log_label = QLabel("Capture Log:")
        log_label.setFont(QFont("Arial", 10, QFont.Bold))
        main_layout.addWidget(log_label)

        # Create scrollable text area for logs
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(200)
        self.log_area.setStyleSheet("""
            background-color: #263238; 
            color: #00ff00; 
            font-family: 'Consolas', monospace; 
            font-size: 9pt;
            border: 1px solid #37474f;
        """)

        scroll_area.setWidget(self.log_area)
        main_layout.addWidget(scroll_area)

        self.setLayout(main_layout)

    def connectSignals(self):
        signals.success.connect(self.show_success)
        signals.error.connect(self.show_error)
        signals.warning.connect(self.show_warning)
        signals.update_status.connect(self.update_status)
        signals.field_stats_updated.connect(self.update_field_stats)

    @pyqtSlot(str)
    def show_success(self, message):
        QMessageBox.information(self, "✅ Success", message)

    @pyqtSlot(str, str)
    def show_error(self, title, message):
        QMessageBox.critical(self, f"❌ {title}", message)

    @pyqtSlot(str, str)
    def show_warning(self, title, message):
        QMessageBox.warning(self, f"⚠️ {title}", message)

    @pyqtSlot(str)
    def update_status(self, message):
        # Add to log area
        current_time = datetime.now().strftime("%H:%M:%S")
        self.log_area.append(f"[{current_time}] {message}")

        # Auto-scroll to bottom
        scrollbar = self.log_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

        # Update status label with last line
        status_line = message.split('\n')[0]  # Get first line for status
        self.status_label.setText(f"Status: {status_line}")

    @pyqtSlot(dict)
    def update_field_stats(self, stats):
        """Update the field statistics display"""
        stats_text = (
            f"📊 Current Page Statistics:\n"
            f"🔢 Total Fields: {stats.get('total_fields', 0)}\n"
            f"🏷️ Named Fields: {stats.get('named_fields', 0)}\n"
            f"🆔 ID Fields: {stats.get('id_fields', 0)}\n"
            f"💬 Placeholder Fields: {stats.get('placeholder_fields', 0)}\n"
            f"⚠️ Required Fields: {stats.get('required_fields', 0)}\n"
            f"♿ ARIA Labeled: {stats.get('aria_labeled_fields', 0)}\n"
            f"📝 Forms: {stats.get('forms_count', 0)}\n"
            f"🖱️ Actions: {stats.get('total_actions', 0)}\n"
        )

        # Add input types breakdown
        input_types = stats.get('input_types', {})
        if input_types:
            stats_text += f"\n🎛️ Field Types:\n"
            for field_type, count in input_types.items():
                stats_text += f"   • {field_type}: {count}\n"

        self.stats_display.setText(stats_text)

    def validate_inputs(self):
        website = self.website_entry.text().strip()
        if website and browser is not None:
            self.start_capture_button.setEnabled(True)
        else:
            self.start_capture_button.setEnabled(False)

    def start_browser_clicked(self):
        if start_browser():
            self.status_label.setText("Status: 🌐 Browser Running")
            self.start_browser_button.setEnabled(False)
            self.stop_browser_button.setEnabled(True)
            self.validate_inputs()
            self.update_status(
                "🚀 Browser started successfully - Ready for capture")
        else:
            self.status_label.setText("Status: ❌ Browser Failed to Start")

    def stop_browser_clicked(self):
        if self.is_capturing:
            self.stop_capturing()

        stop_browser()
        self.status_label.setText("Status: 🛑 Browser Stopped")
        self.start_browser_button.setEnabled(True)
        self.stop_browser_button.setEnabled(False)
        self.start_capture_button.setEnabled(False)
        self.update_status("🛑 Browser stopped")

    def start_capturing(self):
        global TARGET_WEBSITE, stop_capturing, capture_thread

        if not is_browser_alive():
            self.show_error(
                "Browser Error", "Browser is not running. Please start the browser first.")
            return

        TARGET_WEBSITE = self.website_entry.text().strip()
        if not TARGET_WEBSITE.startswith(('http://', 'https://')):
            TARGET_WEBSITE = 'https://' + TARGET_WEBSITE

        stop_capturing = False

        self.is_capturing = True
        self.stop_capture_button.setEnabled(True)
        self.start_capture_button.setEnabled(False)

        # Start enhanced capturing thread
        capture_thread = threading.Thread(
            target=capture_web_actions, daemon=True)
        capture_thread.start()

        self.update_status(f"🎯 Enhanced capture started for {TARGET_WEBSITE}")
        self.status_label.setText("Status: 🔍 Analyzing web flows and forms")

    def stop_capturing(self):
        global stop_capturing

        stop_capturing = True
        self.is_capturing = False
        self.stop_capture_button.setEnabled(False)

        if browser is not None:
            self.start_capture_button.setEnabled(True)

        self.update_status("⏹ Stopping enhanced capture...")
        self.status_label.setText("Status: ✅ Enhanced capture completed")

        # Wait for thread to end
        if capture_thread and capture_thread.is_alive():
            capture_thread.join(timeout=2)

        # Show final summary
        total_pages = len(flows)
        total_fields = sum(stats.get('total_fields', 0)
                           for stats in field_stats.values())
        total_forms = sum(stats.get('forms_count', 0)
                          for stats in field_stats.values())

        summary = (
            f"📊 Capture Summary:\n"
            f"📄 Pages captured: {total_pages}\n"
            f"🔢 Total fields detected: {total_fields}\n"
            f"📝 Total forms found: {total_forms}"
        )

        self.update_status(summary)

    def export_data(self):
        # Export visual and text reports
        export_success = export_enhanced_visualization()

        # Export CSV
        csv_file = self.export_forms_to_csv()

        if export_success:
            files_created = [
                "enhanced_web_flow_capture.png",
                "enhanced_web_flow_report.txt",
                "enhanced_field_data.json"
            ]
            if csv_file:
                files_created.append(csv_file)

            success_msg = (
                "Enhanced data exported successfully!\n\n"
                "Files created:\n" +
                "\n".join([f"• {file}" for file in files_created])
            )

            self.show_success(success_msg)
        else:
            self.show_error("Export Error", "Failed to export enhanced data")

    def export_forms_to_csv(self):
        """Export enhanced form data to CSV with detailed field information"""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_filename = f"enhanced_forms_export_{timestamp}.csv"

            with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = [
                    'URL', 'Page Title', 'Form Index', 'Form ID', 'Form Name', 'Form Action', 'Form Method',
                    'Field Index', 'Field Name', 'Field ID', 'Field Type', 'Field Placeholder',
                    'Field Class', 'Field Required', 'Field Value', 'Field ARIA Label',
                    'Field CSS Selectors', 'Field Source', 'Field Displayed', 'Field Enabled',
                    'Capture Timestamp'
                ]

                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()

                for url, data in flows.items():
                    metadata = data.get('metadata', {})
                    page_title = metadata.get('title', '')
                    forms = metadata.get('forms', [])

                    if not forms:
                        writer.writerow({
                            'URL': url,
                            'Page Title': page_title,
                            'Form Index': 'No forms found',
                            'Capture Timestamp': datetime.fromtimestamp(metadata.get('timestamp', 0)/1000).isoformat()
                        })
                    else:
                        for form_idx, form in enumerate(forms):
                            form_fields = form.get('fields', [])

                            if not form_fields:
                                writer.writerow({
                                    'URL': url,
                                    'Page Title': page_title,
                                    'Form Index': form_idx,
                                    'Form ID': form.get('id', ''),
                                    'Form Name': form.get('name', ''),
                                    'Form Action': form.get('action', ''),
                                    'Form Method': form.get('method', ''),
                                    'Field Index': 'No fields found in form',
                                    'Capture Timestamp': datetime.fromtimestamp(metadata.get('timestamp', 0)/1000).isoformat()
                                })
                            else:
                                for field_idx, field in enumerate(form_fields):
                                    css_selectors = field.get(
                                        'css_selectors', [])
                                    if isinstance(css_selectors, list):
                                        css_selectors_str = ' | '.join(
                                            css_selectors)
                                    else:
                                        css_selectors_str = str(css_selectors)

                                    writer.writerow({
                                        'URL': url,
                                        'Page Title': page_title,
                                        'Form Index': form_idx,
                                        'Form ID': form.get('id', ''),
                                        'Form Name': form.get('name', ''),
                                        'Form Action': form.get('action', ''),
                                        'Form Method': form.get('method', ''),
                                        'Field Index': field_idx,
                                        'Field Name': field.get('name', ''),
                                        'Field ID': field.get('id', ''),
                                        'Field Type': field.get('type', ''),
                                        'Field Placeholder': field.get('placeholder', ''),
                                        'Field Class': field.get('class', ''),
                                        'Field Required': field.get('required', False),
                                        'Field Value': field.get('value', ''),
                                        'Field ARIA Label': field.get('aria_label', ''),
                                        'Field CSS Selectors': css_selectors_str,
                                        'Field Source': field.get('source', 'beautifulsoup'),
                                        'Field Displayed': field.get('displayed', 'Unknown'),
                                        'Field Enabled': not field.get('disabled', False),
                                        'Capture Timestamp': datetime.fromtimestamp(metadata.get('timestamp', 0)/1000).isoformat()
                                    })

            print(f"CSV export created: {csv_filename}")
            return csv_filename

        except Exception as e:
            print(f"Error creating CSV export: {e}")
            return None

    def closeEvent(self, event):
        global stop_capturing

        stop_capturing = True
        stop_browser()
        event.accept()



if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = WebFlowCaptureApp()
    window.show()

    sys.exit(app.exec_())