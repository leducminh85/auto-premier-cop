"""Readable, selectable diagnostics for document input failures."""
from html import escape
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QTextBrowser
from content_parser import ContentParseError


class InputErrorDialog(QDialog):
    def __init__(self, parent, title, error):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 250)
        self.setMinimumSize(480, 220)
        self.setStyleSheet('''
            QDialog { background: #F6F8FC; }
            QTextBrowser { background: white; border: 1px solid #DFE5EE;
                border-radius: 12px; padding: 16px; color: #24324B;
                font-family: 'Segoe UI'; font-size: 14px; }
            QPushButton { background: white; color: #35445D; border: 1px solid #D6DFEB;
                border-radius: 8px; padding: 10px 16px; font-family: 'Segoe UI'; font-size: 13px; }
            QPushButton:hover { background: #EAF2FA; }
            QPushButton:focus { border: 2px solid #1686C4; }
            QPushButton#Primary { background: #167CB5; color: white; border: 1px solid #167CB5; }
            QPushButton#Primary:hover { background: #106796; }
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(14)
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        def safe(text):
            return escape(str(text)).replace('\n', '<br>')
        if isinstance(error, ContentParseError):
            start, end = error.span
            snippet = (safe(error.text[:start]) + '<span style="background-color:#FDE2DE;color:#A32920;font-weight:600;">'
                       + safe(error.text[start:end]) + '</span>' + safe(error.text[end:]))
            self.details.setHtml(
                f'<p><b>{safe(error.location)}</b><br>'
                f'<span style="color:#64748B">{safe(error.path.name)}</span></p>'
                f'<p style="white-space:pre-wrap">{snippet}</p>')
        else:
            self.details.setPlainText(str(error))
        layout.addWidget(self.details, 1)
        actions = QHBoxLayout()
        copy = QPushButton('Sao chép lỗi')
        def copy_error():
            QApplication.clipboard().setText(str(error))
            copy.setText('Đã sao chép')
        copy.clicked.connect(copy_error)
        actions.addWidget(copy)
        actions.addStretch()
        if isinstance(error, ContentParseError):
            open_file = QPushButton('Mở Word để sửa')
            open_file.setObjectName('Primary')
            def open_document():
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(error.path.resolve()))):
                    open_file.setText('Không mở được file')
            open_file.clicked.connect(open_document)
            actions.addWidget(open_file)
        close = QPushButton('Đóng')
        if not isinstance(error, ContentParseError):
            close.setObjectName('Primary')
        close.clicked.connect(self.accept)
        close.setDefault(True)
        actions.addWidget(close)
        layout.addLayout(actions)
