from PyQt5.QtWidgets import (QMainWindow, QWidget, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout, QHBoxLayout, QMessageBox, QTextEdit,
                             QScrollArea, QSplitter, QTabWidget, QTableWidget,
                             QTableWidgetItem, QHeaderView, QStatusBar, QApplication)
from PyQt5.QtCore import pyqtSlot, Qt, QTimer
from PyQt5.QtGui import QFont, QIcon, QTextCursor
import threading
import time
from datetime import datetime

from browser.controller import (start_browser, stop_browser, is_browser_alive,
                                start_capturing, stop_capturing_process)
from ui.flow_dialog import FlowVisualizationDialog
from ui.page_dialog import PageDetailsDialog
from ui.history_dialog import SessionHistoryDialog
from database.graph_db import get_capture_stats
from database.history_manager import history_manager
from util.signals import signals


class WebFlowCaptureApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.connectSignals()
        self.is_capturing = False

        # Setup periodic state update
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.updateState)
        self.timer.start(1000)  # Update every second

    def initUI(self):
        self.setWindowTitle("Web Flow Capture")
        self.setGeometry(100, 100, 900, 700)  # Larger default size

        # Create central widget with scroll area
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Main layout
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # Create a scroll area for most content (excluding status bar)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)
        layout.setSpacing(10)
        # Reduced margins for inner layout
        layout.setContentsMargins(0, 0, 0, 0)

        # Target Website Input
        input_layout = QHBoxLayout()

        self.website_label = QLabel("Target Website:")
        self.website_label.setFixedWidth(100)

        self.website_entry = QLineEdit()
        self.website_entry.setPlaceholderText("example.com")
        self.website_entry.textChanged.connect(self.validate_inputs)

        input_layout.addWidget(self.website_label)
        input_layout.addWidget(self.website_entry)
        layout.addLayout(input_layout)

        # Control buttons
        control_layout = QHBoxLayout()

        # Browser control
        browser_control = QVBoxLayout()
        browser_label = QLabel("Browser Control")
        browser_label.setAlignment(Qt.AlignCenter)
        browser_control.addWidget(browser_label)

        browser_buttons = QHBoxLayout()

        self.start_browser_button = QPushButton("Start Browser")
        self.start_browser_button.clicked.connect(self.start_browser_clicked)
        self.start_browser_button.setFixedHeight(40)
        browser_buttons.addWidget(self.start_browser_button)

        self.stop_browser_button = QPushButton("Stop Browser")
        self.stop_browser_button.clicked.connect(self.stop_browser_clicked)
        self.stop_browser_button.setEnabled(False)
        self.stop_browser_button.setFixedHeight(40)
        browser_buttons.addWidget(self.stop_browser_button)

        browser_control.addLayout(browser_buttons)
        control_layout.addLayout(browser_control)

        # Capture control
        capture_control = QVBoxLayout()
        capture_label = QLabel("Capture Control")
        capture_label.setAlignment(Qt.AlignCenter)
        capture_control.addWidget(capture_label)

        capture_buttons = QHBoxLayout()

        self.start_capture_button = QPushButton("Start Capturing")
        self.start_capture_button.setEnabled(False)
        self.start_capture_button.clicked.connect(self.start_capturing)
        self.start_capture_button.setFixedHeight(40)
        capture_buttons.addWidget(self.start_capture_button)

        self.stop_capture_button = QPushButton("Stop Capturing")
        self.stop_capture_button.setEnabled(False)
        self.stop_capture_button.clicked.connect(self.stop_capturing)
        self.stop_capture_button.setFixedHeight(40)
        capture_buttons.addWidget(self.stop_capture_button)

        capture_control.addLayout(capture_buttons)
        control_layout.addLayout(capture_control)

        # Data control
        data_control = QVBoxLayout()
        data_label = QLabel("Data Control")
        data_label.setAlignment(Qt.AlignCenter)
        data_control.addWidget(data_label)

        data_buttons = QHBoxLayout()

        self.view_data_button = QPushButton("View Current Data")
        self.view_data_button.clicked.connect(self.view_captured_data)
        self.view_data_button.setFixedHeight(40)
        data_buttons.addWidget(self.view_data_button)

        self.view_history_button = QPushButton("View History")
        self.view_history_button.clicked.connect(self.view_history)
        self.view_history_button.setFixedHeight(40)
        data_buttons.addWidget(self.view_history_button)

        data_control.addLayout(data_buttons)
        control_layout.addLayout(data_control)

        layout.addLayout(control_layout)

        # Tabbed information area
        self.tabs = QTabWidget()

        # Instructions tab
        instructions_tab = QWidget()
        instructions_layout = QVBoxLayout(instructions_tab)

        instructions_text = (
            "Instructions:\n\n"
            "1. Enter target website URL in the field above\n"
            "2. Click 'Start Browser' to launch a controlled Chrome browser\n"
            "3. Click 'Start Capturing' to begin recording web actions\n"
            "4. Browse normally in the opened browser window\n"
            "   - All pages, tabs, and popups will be captured automatically\n"
            "   - No proxy configuration required\n"
            "5. Click 'Stop Capturing' when finished\n"
            "6. Click 'View Current Data' to analyze the recorded web flows\n"
            "7. Click 'View History' to see previous capture sessions\n\n"
            "Notes:\n"
            "- The browser window must remain open during capture\n"
            "- All actions across tabs and popups are recorded\n"
            "- JavaScript alerts are automatically accepted and recorded\n"
        )

        instructions_label = QLabel(instructions_text)
        instructions_label.setWordWrap(True)
        instructions_layout.addWidget(instructions_label)

        self.tabs.addTab(instructions_tab, "Instructions")

        # Stats tab
        stats_tab = QWidget()
        stats_layout = QVBoxLayout(stats_tab)

        self.stats_table = QTableWidget()
        self.stats_table.setColumnCount(2)
        self.stats_table.setHorizontalHeaderLabels(["Metric", "Value"])
        self.stats_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.stats_table.setAlternatingRowColors(True)
        self.stats_table.verticalHeader().setVisible(False)

        stats_layout.addWidget(self.stats_table)
        self.tabs.addTab(stats_tab, "Statistics")

        # Log tab
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        log_layout.addWidget(self.log_area)

        self.tabs.addTab(log_tab, "Log")

        layout.addWidget(self.tabs)

        # Set the scroll widget to the scroll area
        scroll_area.setWidget(scroll_widget)
        main_layout.addWidget(scroll_area)

        # Status bar
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)

        # Status elements
        self.browser_status = QLabel("Browser: Not Running")
        self.capture_status = QLabel("Capture: Inactive")
        self.session_status = QLabel("Session: None")
        self.statusBar.addPermanentWidget(self.browser_status)
        self.statusBar.addPermanentWidget(self.capture_status)
        self.statusBar.addPermanentWidget(self.session_status)

        # Initial update of statistics
        self.updateStats()

    def connectSignals(self):
        # Connect worker signals to UI methods
        signals.success.connect(self.show_success)
        signals.error.connect(self.show_error)
        signals.warning.connect(self.show_warning)
        signals.update_status.connect(self.update_status)
        signals.page_captured.connect(self.on_page_captured)
        signals.alert_captured.connect(self.on_alert_captured)
        signals.new_tab_detected.connect(self.on_new_tab)

    def updateState(self):
        """Update UI state based on browser and capture status"""
        # Check browser state
        browser_running = is_browser_alive()
        if browser_running:
            self.browser_status.setText("Browser: Running")
            self.start_browser_button.setEnabled(False)
            self.stop_browser_button.setEnabled(True)

            # Only enable start capture if we have a URL
            if self.website_entry.text().strip():
                self.start_capture_button.setEnabled(not self.is_capturing)
            else:
                self.start_capture_button.setEnabled(False)
        else:
            self.browser_status.setText("Browser: Not Running")
            self.start_browser_button.setEnabled(True)
            self.stop_browser_button.setEnabled(False)
            self.start_capture_button.setEnabled(False)

        # Update capture status
        if self.is_capturing:
            self.capture_status.setText("Capture: Active")
            self.stop_capture_button.setEnabled(True)
        else:
            self.capture_status.setText("Capture: Inactive")
            self.stop_capture_button.setEnabled(False)

        # Update session status
        if history_manager.current_session:
            session_id = history_manager.current_session.id
            self.session_status.setText(f"Session: {session_id}")
        else:
            self.session_status.setText("Session: None")

        # Update statistics periodically
        if self.is_capturing and self.tabs.currentIndex() == 1:  # Stats tab
            self.updateStats()

    def updateStats(self):
        """Update the statistics table with comprehensive current data"""
        try:
            # Get current session ID
            session_id = None
            if history_manager.current_session:
                session_id = history_manager.current_session.id

            stats = get_capture_stats(session_id)

            # Clear table
            self.stats_table.setRowCount(0)

            # Add comprehensive capture overview
            self.addStatsRow("═══ CAPTURE OVERVIEW ═══", "")
            self.addStatsRow("📄 Pages Captured", str(stats.get("pages", 0)))
            self.addStatsRow("📝 Forms Detected", str(stats.get("forms", 0)))
            self.addStatsRow("⚠️ Alerts Captured", str(stats.get("alerts", 0)))
            self.addStatsRow("🔗 Navigation Flows", str(stats.get("flows", 0)))

            # Add enhanced field detection statistics
            total_fields = stats.get("total_fields", 0)
            if total_fields > 0:
                self.addStatsRow("═══ FIELD DETECTION RESULTS ═══", "")
                self.addStatsRow("🔢 Total Fields Found", str(total_fields))
                self.addStatsRow("🏷️ Named Fields", str(
                    stats.get("named_fields", 0)))
                self.addStatsRow("🆔 ID-based Fields",
                                 str(stats.get("id_fields", 0)))
                self.addStatsRow("💬 Placeholder Fields", str(
                    stats.get("placeholder_fields", 0)))
                self.addStatsRow("⚠️ Required Fields", str(
                    stats.get("required_fields", 0)))
                self.addStatsRow("♿ ARIA Labeled Fields", str(
                    stats.get("aria_labeled_fields", 0)))

                # Calculate field identification success rate
                named_fields = stats.get("named_fields", 0)
                id_fields = stats.get("id_fields", 0)
                identifiable_fields = named_fields + id_fields

                if total_fields > 0:
                    success_rate = round(
                        (identifiable_fields / total_fields) * 100, 1)
                    self.addStatsRow(
                        "🎯 Field ID Success Rate", f"{success_rate}% ({identifiable_fields}/{total_fields})")

            # Add advanced field attributes
            class_fields = stats.get("class_fields", 0)
            data_fields = stats.get("data_attribute_fields", 0)

            if class_fields > 0 or data_fields > 0:
                self.addStatsRow("═══ ADVANCED ATTRIBUTES ═══", "")
                if class_fields > 0:
                    self.addStatsRow("🎨 Class-based Fields", str(class_fields))
                if data_fields > 0:
                    self.addStatsRow(
                        "📊 Data Attribute Fields", str(data_fields))

            # Add enhanced detection method information
            selenium_fields = stats.get("selenium_fields", 0)
            shadow_fields = stats.get("shadow_dom_fields", 0)

            if selenium_fields > 0 or shadow_fields > 0:
                self.addStatsRow("═══ ENHANCED DETECTION ═══", "")
                if selenium_fields > 0:
                    self.addStatsRow("🤖 Selenium Detected",
                                     f"{selenium_fields} fields")
                if shadow_fields > 0:
                    self.addStatsRow("🌑 Shadow DOM Detected",
                                     f"{shadow_fields} fields")

                detection_method = "✅ Multi-Method Enhanced"
            else:
                detection_method = "⚠️ Basic HTML Only"

            self.addStatsRow("🔍 Detection Method", detection_method)

            # Add detailed input type breakdown with icons
            input_types = stats.get("input_types", {})
            if input_types:
                self.addStatsRow("═══ INPUT TYPE BREAKDOWN ═══", "")

                # Group and display input types with appropriate icons
                for input_type, count in sorted(input_types.items()):
                    # Assign icons based on input type
                    if input_type in ["text", "email", "password", "search", "url", "tel"]:
                        icon = "🔤"
                    elif input_type in ["checkbox", "radio"]:
                        icon = "☑️"
                    elif input_type == "select":
                        icon = "📋"
                    elif input_type in ["number", "range"]:
                        icon = "🔢"
                    elif input_type in ["date", "datetime", "datetime-local", "time"]:
                        icon = "📅"
                    elif input_type == "file":
                        icon = "📎"
                    elif input_type in ["button", "submit", "reset"]:
                        icon = "🔘"
                    else:
                        icon = "❓"

                    self.addStatsRow(
                        f"  {icon} {input_type.capitalize()}", str(count))

                # Add input type summary
                text_types = ["text", "email",
                              "password", "search", "url", "tel"]
                choice_types = ["checkbox", "radio", "select"]

                text_count = sum(input_types.get(t, 0) for t in text_types)
                choice_count = sum(input_types.get(t, 0) for t in choice_types)

                if text_count > 0 or choice_count > 0:
                    self.addStatsRow("─── Type Categories ───", "")
                    if text_count > 0:
                        self.addStatsRow("📝 Text Input Types", str(text_count))
                    if choice_count > 0:
                        self.addStatsRow("✅ Selection Types",
                                         str(choice_count))

            # Add quality assessment
            if total_fields > 0:
                self.addStatsRow("═══ QUALITY ASSESSMENT ═══", "")

                # Accessibility score
                aria_fields = stats.get("aria_labeled_fields", 0)
                if aria_fields > 0:
                    aria_percentage = round(
                        (aria_fields / total_fields) * 100, 1)
                    accessibility_level = "Excellent" if aria_percentage > 80 else "Good" if aria_percentage > 50 else "Fair" if aria_percentage > 20 else "Poor"
                    self.addStatsRow(
                        "♿ Accessibility Score", f"{aria_percentage}% - {accessibility_level}")
                else:
                    self.addStatsRow("♿ Accessibility Score",
                                     "0% - Needs Improvement")

                # Form quality assessment
                forms_count = stats.get("forms", 0)
                if forms_count > 0:
                    avg_fields = total_fields / forms_count if forms_count > 0 else 0
                    form_complexity = "Simple" if avg_fields < 5 else "Moderate" if avg_fields < 15 else "Complex"
                    self.addStatsRow(
                        "📋 Form Complexity", f"{form_complexity} ({avg_fields:.1f} fields/form)")

                # Field identification quality
                if identifiable_fields > 0:
                    if success_rate >= 80:
                        id_quality = "✅ Excellent"
                    elif success_rate >= 60:
                        id_quality = "✨ Good"
                    elif success_rate >= 40:
                        id_quality = "⚠️ Fair"
                    else:
                        id_quality = "❌ Poor"
                    self.addStatsRow("🎯 ID Quality", id_quality)

            # Add session information with enhanced details
            if session_id:
                self.addStatsRow("═══ SESSION DETAILS ═══", "")
                self.addStatsRow(
                    "🏷️ Session ID", session_id[:12] + "..." if len(session_id) > 12 else session_id)

                # Add session start time and duration
                if history_manager.current_session:
                    start_time = history_manager.current_session.formatted_start_time
                    self.addStatsRow("🕐 Started At", start_time)

                    duration = history_manager.current_session.duration
                    self.addStatsRow("⏱️ Duration", duration)

                    # Add session website
                    website = history_manager.current_session.website
                    if website:
                        display_website = website[:40] + \
                            "..." if len(website) > 40 else website
                        self.addStatsRow("🌐 Target Website", display_website)

            # Add performance metrics
            if stats.get("pages", 0) > 0:
                self.addStatsRow("═══ PERFORMANCE METRICS ═══", "")

                # Fields per page ratio
                pages = stats.get("pages", 0)
                if pages > 0 and total_fields > 0:
                    fields_per_page = round(total_fields / pages, 1)
                    self.addStatsRow("📊 Fields per Page",
                                     f"{fields_per_page} avg")

                # Forms per page ratio
                forms = stats.get("forms", 0)
                if pages > 0 and forms > 0:
                    forms_per_page = round(forms / pages, 1)
                    self.addStatsRow("📝 Forms per Page",
                                     f"{forms_per_page} avg")

                # Coverage metrics
                pages_with_forms = min(forms, pages)  # Rough estimate
                if pages > 0:
                    form_coverage = round((pages_with_forms / pages) * 100, 1)
                    self.addStatsRow("📈 Form Coverage",
                                     f"{form_coverage}% of pages")

            # Add recommendations based on data
            if total_fields > 0 or stats.get("pages", 0) > 0:
                self.addStatsRow("═══ RECOMMENDATIONS ═══", "")

                # Accessibility recommendations
                if aria_fields == 0 and total_fields > 0:
                    self.addStatsRow(
                        "💡 Accessibility", "Add ARIA labels to improve accessibility")
                elif aria_fields < total_fields * 0.5 and total_fields > 0:
                    self.addStatsRow("💡 Accessibility",
                                     "Consider adding more ARIA labels")

                # Field identification recommendations
                if success_rate < 60 and total_fields > 0:
                    self.addStatsRow("💡 Field Identification",
                                     "Add names or IDs to improve automation")

                # Detection method recommendations
                if selenium_fields == 0 and shadow_fields == 0 and total_fields > 0:
                    self.addStatsRow(
                        "💡 Detection", "Enhanced detection could find more fields")

            # Add current timestamp for reference
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.addStatsRow("🕒 Last Updated", current_time)

            # Add error information if present
            if stats.get("error"):
                self.addStatsRow("═══ ISSUES ═══", "")
                self.addStatsRow("❌ Database Error", stats.get("error", ""))

        except Exception as e:
            print(f"Error updating stats: {e}")
            # Add error info to stats table
            self.stats_table.setRowCount(0)
            self.addStatsRow("❌ Error", f"Failed to load statistics: {str(e)}")
            self.addStatsRow("🔄 Action", "Click Refresh to try again")

    def addStatsRow(self, metric, value):
        """Add a row to the stats table"""
        row = self.stats_table.rowCount()
        self.stats_table.insertRow(row)
        self.stats_table.setItem(row, 0, QTableWidgetItem(metric))
        self.stats_table.setItem(row, 1, QTableWidgetItem(value))

    @pyqtSlot(str)
    def show_success(self, message):
        QMessageBox.information(self, "Success", message)

    @pyqtSlot(str, str)
    def show_error(self, title, message):
        QMessageBox.critical(self, title, message)

    @pyqtSlot(str, str)
    def show_warning(self, title, message):
        QMessageBox.warning(self, title, message)

    @pyqtSlot(str)
    def update_status(self, message):
        # Switch to log tab for important messages
        if "Error" in message or "Started" in message or "Stopped" in message:
            self.tabs.setCurrentIndex(2)  # Log tab

        # Append to log with timestamp
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_area.append(f"[{timestamp}] {message}")

        # Scroll to bottom
        self.log_area.moveCursor(QTextCursor.End)

        # Show in status bar too
        self.statusBar.showMessage(message, 3000)

    @pyqtSlot(str, str)
    def on_page_captured(self, url, title):
        """Called when a page is captured"""
        self.update_status(f"Captured: {title} ({url})")
        self.updateStats()

    @pyqtSlot(str, str)
    def on_alert_captured(self, url, message):
        """Called when an alert is captured"""
        self.update_status(f"Alert captured on {url}: {message}")
        self.updateStats()

    @pyqtSlot(str)
    def on_new_tab(self, url):
        """Called when a new tab is detected"""
        self.update_status(f"New tab opened: {url}")

    def validate_inputs(self):
        website = self.website_entry.text().strip()
        if website and is_browser_alive() and not self.is_capturing:
            self.start_capture_button.setEnabled(True)
        else:
            self.start_capture_button.setEnabled(False)

    def start_browser_clicked(self):
        if start_browser():
            self.browser_status.setText("Browser: Running")
            self.start_browser_button.setEnabled(False)
            self.stop_browser_button.setEnabled(True)
            self.validate_inputs()
            self.update_status("Browser started successfully")
        else:
            self.browser_status.setText("Browser: Failed to Start")
            self.update_status("Failed to start browser")

    def stop_browser_clicked(self):
        # Ensure capturing is stopped first
        if self.is_capturing:
            self.stop_capturing()

        stop_browser()
        self.browser_status.setText("Browser: Not Running")
        self.start_browser_button.setEnabled(True)
        self.stop_browser_button.setEnabled(False)
        self.start_capture_button.setEnabled(False)
        self.update_status("Browser stopped")

    def start_capturing(self):
        # Check if browser is running first
        if not is_browser_alive():
            self.show_error(
                "Browser Error", "Browser is not running. Please start the browser first.")
            return

        target_url = self.website_entry.text()

        # End any existing session first to be sure
        history_manager.end_current_session()

        # Start a new session in the history manager
        session = history_manager.start_session(target_url)
        self.session_status.setText(f"Session: {session.id}")

        self.is_capturing = True
        self.capture_status.setText("Capture: Active")
        self.stop_capture_button.setEnabled(True)
        self.start_capture_button.setEnabled(False)

        # Start capturing thread
        start_capturing(target_url)

        # Update UI
        self.update_status(f"Started capturing for {target_url}")

    def stop_capturing(self):
        # End current session
        if history_manager.end_current_session():
            self.update_status("Session saved to history")

        # Stop capturing process
        stop_capturing_process()

        self.is_capturing = False
        self.capture_status.setText("Capture: Inactive")
        self.stop_capture_button.setEnabled(False)

        if is_browser_alive():
            self.start_capture_button.setEnabled(True)

        # Update UI
        self.update_status("Capture stopped")

        # Update stats one last time
        self.updateStats()

    def view_captured_data(self):
        # Get current session ID
        session_id = None
        if history_manager.current_session:
            session_id = history_manager.current_session.id
        else:
            # If no current session, try to get the most recent session
            recent_sessions = history_manager.get_all_sessions(
                sort_by="start_time", reverse=True)
            if recent_sessions:
                session_id = recent_sessions[0].id
                self.update_status(f"Using most recent session: {session_id}")

        if not session_id:
            self.show_warning(
                "No Session", "No active or recent session found. Start capturing or select a session from history.")
            return

        # Create the flow visualization dialog
        flow_dialog = FlowVisualizationDialog(self, session_id)
        flow_dialog.pageSelected.connect(self.show_page_details)
        flow_dialog.exec_()

    def view_history(self):
        # Open the history dialog
        history_dialog = SessionHistoryDialog(self)
        history_dialog.sessionSelected.connect(self.view_session_data)
        history_dialog.exec_()

    def view_session_data(self, session_id):
        # Create the flow visualization dialog for the selected session
        flow_dialog = FlowVisualizationDialog(self, session_id)
        flow_dialog.pageSelected.connect(self.show_page_details)
        flow_dialog.exec_()

    def show_page_details(self, url):
        # Show details dialog for the selected page
        details_dialog = PageDetailsDialog(url, self)
        details_dialog.exec_()

    def closeEvent(self, event):
        # End any active session to ensure it's saved
        if history_manager.current_session and not history_manager.current_session.end_time:
            self.update_status("Saving current session before exit...")
            history_manager.end_current_session()
            history_manager.save_history()  # Extra save to be sure

        # Ensure capturing is stopped first
        if self.is_capturing:
            self.stop_capturing()

        # Stop browser
        stop_browser()

        # Stop timer
        self.timer.stop()

        # Make sure databases are closed properly
        from database.graph_db import close_neo4j_connection
        from database.vector_db import close_pg_connection

        close_neo4j_connection()
        close_pg_connection()

        event.accept()
