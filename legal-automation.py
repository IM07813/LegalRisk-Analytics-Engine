import os
import re
import json
import time
import logging
import sqlite3
import hashlib
import threading
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import requests
import yaml

# Configuration Defaults
DEFAULT_CONFIG = {
    'api_token': '',
    'watch_dir': './docs',
    'output_dir': './reports',
    'db_path': 'legal_analyses.db',
    'max_workers': 4,
    'request_timeout': 30,
    'max_retries': 5,
    'risk_weights': {
        'legal': 1.5,
        'financial': 1.4,
        'reputation': 1.3,
        'operational': 1.2
    },
    'risk_thresholds': {
        'high': 0.7,
        'medium': 0.4
    }
}

class ConfigManager:
    def __init__(self, config_path: str = 'config.yaml'):
        self.config_path = config_path
        self.config = self._load_config()
        self._validate()

    def _load_config(self) -> Dict:
        try:
            with open(self.config_path) as f:
                user_config = yaml.safe_load(f) or {}
                return {**DEFAULT_CONFIG, **user_config}
        except Exception as e:
            logging.error(f"Config error: {str(e)}")
            return DEFAULT_CONFIG

    def _validate(self):
        Path(self.config['watch_dir']).mkdir(exist_ok=True, parents=True)
        Path(self.config['output_dir']).mkdir(exist_ok=True, parents=True)

class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        try:
            with self.connect() as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS documents
                                (id TEXT PRIMARY KEY,
                                 filename TEXT,
                                 processed_at TEXT,
                                 risk_score REAL,
                                 risk_category TEXT,
                                 summary TEXT,
                                 document_hash TEXT)''')
        except sqlite3.Error as e:
            logging.error(f"DB init failed: {str(e)}")

    def connect(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def document_exists(self, doc_hash: str) -> bool:
        try:
            with self.connect() as conn:
                cursor = conn.execute('SELECT 1 FROM documents WHERE document_hash = ?', (doc_hash,))
                return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logging.error(f"DB check failed: {str(e)}")
            return False

    def save_document(self, doc_id: str, filename: str, analysis: Dict, doc_hash: str):
        try:
            with self.connect() as conn:
                conn.execute('''INSERT OR REPLACE INTO documents 
                                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                             (doc_id, filename, datetime.now().isoformat(),
                              analysis['risk_score'], analysis['risk_category'],
                              json.dumps(analysis['summary']), doc_hash))
                conn.commit()
        except sqlite3.Error as e:
            logging.error(f"DB save failed: {str(e)}")

class DocumentAnalyzer:
    def __init__(self, api_token: str, config: Dict):
        self.api_token = api_token
        self.config = config
        self.session = requests.Session()
        self.executor = ThreadPoolExecutor(max_workers=config['max_workers'])

    def generate_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def analyze(self, text: str) -> Dict:
        try:
            sections = self._split_into_sections(text)
            futures = [self.executor.submit(self.analyze_section, section) for section in sections]
            
            analyzed_sections = []
            for future in as_completed(futures):
                result = future.result()
                if result:
                    analyzed_sections.append(result)
            
            return self._compile_report(analyzed_sections)
        except Exception as e:
            logging.error(f"Analysis failed: {str(e)}")
            return self._error_response()

    def _split_into_sections(self, text: str) -> List[Dict]:
        sections = []
        current_section = {'title': 'Introduction', 'content': ''}
        for line in text.split('\n'):
            if re.match(r'^Section \d+:', line):
                if current_section['content'].strip():
                    sections.append(current_section)
                title = line.split(':', 1)[-1].strip()
                current_section = {'title': title, 'content': ''}
            else:
                current_section['content'] += line + '\n'
        if current_section['content'].strip():
            sections.append(current_section)
        return sections

    def analyze_section(self, section: Dict) -> Optional[Dict]:
        try:
            sentiment = self._call_sentiment_api(section['content'])
            keywords = self._detect_keywords(section['content'])
            return {
                'title': section['title'],
                'sentiment': sentiment,
                'keywords': keywords,
                'risk_score': self._calculate_section_risk(sentiment, keywords)
            }
        except Exception as e:
            logging.error(f"Section analysis failed: {str(e)}")
            return None

    def _call_sentiment_api(self, text: str) -> Dict:
        for attempt in range(self.config['max_retries']):
            try:
                response = self.session.post(
                    'https://api-inference.huggingface.co/models/ProsusAI/finbert',
                    headers={'Authorization': f'Bearer {self.api_token}'},
                    json={'inputs': text[:2000]},
                    timeout=self.config['request_timeout']
                )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list) and len(data) > 0:
                    top_label = max(data[0], key=lambda x: x['score'])
                    return {'label': top_label['label'], 'score': top_label['score']}
                raise ValueError("Unexpected API response format")
            except requests.RequestException as e:
                if attempt == self.config['max_retries'] - 1:
                    raise
                time.sleep(2 ** attempt)
        return {'label': 'ERROR', 'score': 0.0}

    def _detect_keywords(self, text: str) -> List[str]:
        detected = []
        text_lower = text.lower()
        for keyword, weight in self.config['risk_weights'].items():
            if keyword in text_lower:
                detected.append(keyword)
        return detected

    def _calculate_section_risk(self, sentiment: Dict, keywords: List[str]) -> float:
        risk = 0.0
        if sentiment['label'] == 'negative':
            risk = sentiment['score']
        elif sentiment['label'] == 'positive':
            risk = 1 - sentiment['score']
        else:
            risk = 0.5 * (1 - sentiment['score'])
        
        for keyword in keywords:
            risk *= self.config['risk_weights'].get(keyword, 1.0)
        
        return min(max(risk, 0.0), 1.0)

    def _compile_report(self, sections: List[Dict]) -> Dict:
        if not sections:
            return self._error_response()
        
        total_risk = sum(s['risk_score'] for s in sections) / len(sections)
        weighted_risk = total_risk
        risk_category = self._determine_risk_category(weighted_risk)
        
        key_findings = [
            f"{s['title']}: {s['sentiment']['label']} sentiment ({s['risk_score']:.2f})"
            for s in sections if s['risk_score'] >= self.config['risk_thresholds']['high']
        ][:5]
        
        risk_factors = []
        negative_count = sum(1 for s in sections if s['sentiment']['label'] == 'negative')
        if negative_count:
            risk_factors.append(f"{negative_count} critical sections with negative sentiment")
        if weighted_risk >= self.config['risk_thresholds']['high']:
            risk_factors.append("Severe overall risk requiring immediate action")
        elif weighted_risk >= self.config['risk_thresholds']['medium']:
            risk_factors.append("Elevated risk needing urgent review")
        
        return {
            'risk_score': round(weighted_risk, 2),
            'risk_category': risk_category,
            'summary': {
                'sections_analyzed': len(sections),
                'total_keywords': sum(len(s['keywords']) for s in sections)
            },
            'key_findings': key_findings,
            'risk_factors': risk_factors
        }

    def _determine_risk_category(self, score: float) -> str:
        if score >= self.config['risk_thresholds']['high']:
            return 'high'
        elif score >= self.config['risk_thresholds']['medium']:
            return 'medium'
        return 'low'

    def _error_response(self) -> Dict:
        return {
            'risk_score': 0.0,
            'risk_category': 'error',
            'summary': {'error': 'Analysis failed'},
            'key_findings': [],
            'risk_factors': []
        }

class LegalAnalysisSystem(FileSystemEventHandler):
    def __init__(self, config: Dict):
        self.config = config
        self.db = DatabaseManager(config['db_path'])
        self.analyzer = DocumentAnalyzer(config['api_token'], config)
        self.observer = Observer()
        self.executor = ThreadPoolExecutor(max_workers=config['max_workers'])
        self._setup_logging()
        self.risk_weight_sum = sum(config['risk_weights'].values())

    def _setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('legal_analysis.log'),
                logging.StreamHandler()
            ]
        )

    def on_created(self, event):
        if not event.is_directory:
            self.executor.submit(self.process_document, event.src_path)

    def process_document(self, file_path: str):
        try:
            logging.info(f"Processing: {file_path}")
            doc_id = Path(file_path).stem
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            doc_hash = self.analyzer.generate_hash(content)
            if self.db.document_exists(doc_hash):
                logging.info(f"Skipping duplicate document: {file_path}")
                return
            
            analysis = self.analyzer.analyze(content)
            self.db.save_document(doc_id, file_path, analysis, doc_hash)
            self.generate_report(doc_id, analysis)
            logging.info(f"Completed analysis for: {file_path}")
        except Exception as e:
            logging.error(f"Processing failed for {file_path}: {str(e)}")

    def generate_report(self, doc_id: str, analysis: Dict):
        try:
            report = f"""# Legal Risk Analysis Report
**Document ID**: {doc_id}
**Date**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Risk Score**: {analysis['risk_score']:.2f} ({analysis['risk_category'].upper()})

## Key Findings
{chr(10).join('● ' + finding for finding in analysis['key_findings']) or 'No critical findings'}

## Risk Factors
{chr(10).join('⚠️ ' + factor for factor in analysis['risk_factors']) or 'No significant risk factors detected'}

## Analysis Summary
- Sections analyzed: {analysis['summary'].get('sections_analyzed', 0)}
- Keywords detected: {analysis['summary'].get('total_keywords', 0)}
"""
            output_path = Path(self.config['output_dir']) / f"{doc_id}_report.md"
            output_path.write_text(report)
        except Exception as e:
            logging.error(f"Report generation failed: {str(e)}")

    def shutdown(self):
        self.executor.shutdown(wait=True)
        self.observer.stop()
        self.observer.join()

    def start_monitoring(self):
        self.observer.schedule(self, self.config['watch_dir'], recursive=True)
        self.observer.start()
        logging.info(f"Started monitoring: {self.config['watch_dir']}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()
            logging.info("System shutdown complete")

if __name__ == "__main__":
    try:
        config = ConfigManager().config
        system = LegalAnalysisSystem(config)
        system.start_monitoring()
    except Exception as e:
        logging.critical(f"Fatal initialization error: {str(e)}")
        exit(1)
