#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║                    NEXUSAI PRO v2.0                         ║
║            God-Level AI Platform Backend                     ║
║     Rivals Gemini, ChatGPT & Claude in UI/UX                ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import hashlib
import secrets
import logging
import threading
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote_plus

import requests
import httpx
import bleach
import markdown
from dotenv import load_dotenv

from flask import (
    Flask, render_template, request, jsonify, redirect,
    url_for, session, Response, stream_with_context, abort,
    make_response
)
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_socketio import SocketIO, emit
from sqlalchemy import text, desc, func
from groq import Groq

load_dotenv()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# APP CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 'sqlite:///nexusai_dev.db'
).replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
    'pool_size': 10,
    'max_overflow': 20
}
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login_page'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('NexusAI')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DATABASE MODELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active_user = db.Column(db.Boolean, default=True)
    is_banned = db.Column(db.Boolean, default=False)
    avatar_url = db.Column(db.String(500), default='')
    theme = db.Column(db.String(20), default='dark')
    language = db.Column(db.String(10), default='en')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, default=datetime.utcnow)
    total_messages = db.Column(db.Integer, default=0)
    total_tokens_used = db.Column(db.BigInteger, default=0)
    chats = db.relationship('Chat', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    
    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    
    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)
    
    def get_id(self):
        return str(self.id)
    
    @property
    def is_active(self):
        return self.is_active_user and not self.is_banned


class Chat(db.Model):
    __tablename__ = 'chats'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    title = db.Column(db.String(200), default='New Chat')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_pinned = db.Column(db.Boolean, default=False)
    is_archived = db.Column(db.Boolean, default=False)
    messages = db.relationship('Message', backref='chat', lazy='dynamic', cascade='all, delete-orphan',
                                order_by='Message.created_at')


class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey('chats.id', ondelete='CASCADE'), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)  # user, assistant, system
    content = db.Column(db.Text, nullable=False)
    tokens_used = db.Column(db.Integer, default=0)
    model_used = db.Column(db.String(100), default='')
    sources_used = db.Column(db.Text, default='')  # JSON list of sources
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reaction = db.Column(db.String(10), default='')  # like, dislike


class SiteSettings(db.Model):
    __tablename__ = 'site_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class APIKey(db.Model):
    __tablename__ = 'api_keys'
    id = db.Column(db.Integer, primary_key=True)
    service_name = db.Column(db.String(100), unique=True, nullable=False, index=True)
    api_key = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    usage_count = db.Column(db.Integer, default=0)
    last_used = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AnalyticsEvent(db.Model):
    __tablename__ = 'analytics'
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(50), nullable=False, index=True)
    event_data = db.Column(db.Text, default='{}')
    user_id = db.Column(db.Integer, nullable=True)
    ip_address = db.Column(db.String(50), default='')
    user_agent = db.Column(db.String(500), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class SystemLog(db.Model):
    __tablename__ = 'system_logs'
    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String(20), default='info')
    message = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(100), default='system')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CustomCommand(db.Model):
    __tablename__ = 'custom_commands'
    id = db.Column(db.Integer, primary_key=True)
    command = db.Column(db.String(100), unique=True, nullable=False)
    response = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Announcement(db.Model):
    __tablename__ = 'announcements'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    priority = db.Column(db.String(20), default='normal')  # low, normal, high, critical
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SETTINGS HELPER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_setting(key, default=''):
    s = SiteSettings.query.filter_by(key=key).first()
    return s.value if s else default

def set_setting(key, value):
    s = SiteSettings.query.filter_by(key=key).first()
    if s:
        s.value = str(value)
    else:
        s = SiteSettings(key=key, value=str(value))
        db.session.add(s)
    db.session.commit()

def get_api_key(service_name):
    k = APIKey.query.filter_by(service_name=service_name, is_active=True).first()
    if k:
        k.usage_count += 1
        k.last_used = datetime.utcnow()
        db.session.commit()
        return k.api_key
    if service_name == 'groq':
        return os.environ.get('GROQ_API_KEY', '')
    return ''

def log_event(event_type, data='', user_id=None):
    try:
        evt = AnalyticsEvent(
            event_type=event_type,
            event_data=json.dumps(data) if isinstance(data, dict) else str(data),
            user_id=user_id,
            ip_address=request.remote_addr if request else '',
            user_agent=str(request.user_agent) if request else ''
        )
        db.session.add(evt)
        db.session.commit()
    except Exception:
        db.session.rollback()

def sys_log(message, level='info', source='system'):
    try:
        log = SystemLog(level=level, message=message, source=source)
        db.session.add(log)
        db.session.commit()
    except Exception:
        db.session.rollback()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADMIN REQUIRED DECORATOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# KNOWLEDGE ENGINE - 200+ OPEN SOURCE APIs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KnowledgeEngine:
    """Smart RAG Engine querying 200+ open-source databases"""
    
    SOURCES = {
        # ── ENCYCLOPEDIAS & GENERAL KNOWLEDGE ──
        'wikipedia': {'url': 'https://en.wikipedia.org/api/rest_v1/page/summary/{query}', 'category': 'encyclopedia'},
        'wikipedia_search': {'url': 'https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&format=json&srlimit=3', 'category': 'encyclopedia'},
        'wikidata': {'url': 'https://www.wikidata.org/w/api.php?action=wbsearchentities&search={query}&language=en&format=json&limit=5', 'category': 'encyclopedia'},
        'dbpedia': {'url': 'https://lookup.dbpedia.org/api/search?query={query}&format=json&maxResults=3', 'category': 'encyclopedia'},
        'duckduckgo': {'url': 'https://api.duckduckgo.com/?q={query}&format=json&no_html=1', 'category': 'search'},
        
        # ── SCIENCE & RESEARCH ──
        'arxiv': {'url': 'http://export.arxiv.org/api/query?search_query=all:{query}&max_results=3', 'category': 'science'},
        'pubmed': {'url': 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={query}&retmode=json&retmax=3', 'category': 'medicine'},
        'crossref': {'url': 'https://api.crossref.org/works?query={query}&rows=3', 'category': 'science'},
        'semanticscholar': {'url': 'https://api.semanticscholar.org/graph/v1/paper/search?query={query}&limit=3', 'category': 'science'},
        'openalex': {'url': 'https://api.openalex.org/works?search={query}&per_page=3', 'category': 'science'},
        'core': {'url': 'https://api.core.ac.uk/v3/search/works?q={query}&limit=3', 'category': 'science'},
        'doaj': {'url': 'https://doaj.org/api/search/articles/{query}?pageSize=3', 'category': 'science'},
        'unpaywall': {'url': 'https://api.unpaywall.org/v2/search?query={query}', 'category': 'science'},
        'europe_pmc': {'url': 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={query}&format=json&pageSize=3', 'category': 'science'},
        'nasa_images': {'url': 'https://images-api.nasa.gov/search?q={query}&media_type=image', 'category': 'science'},
        'nasa_patents': {'url': 'https://api.nasa.gov/techtransfer/patent/?engine&api_key=DEMO_KEY&query={query}', 'category': 'science'},
        
        # ── TECHNOLOGY & CODE ──
        'github_repos': {'url': 'https://api.github.com/search/repositories?q={query}&sort=stars&per_page=3', 'category': 'code'},
        'github_code': {'url': 'https://api.github.com/search/code?q={query}&per_page=3', 'category': 'code'},
        'stackoverflow': {'url': 'https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q={query}&site=stackoverflow&pagesize=3', 'category': 'code'},
        'stackexchange': {'url': 'https://api.stackexchange.com/2.3/search?order=desc&sort=activity&intitle={query}&site=superuser&pagesize=3', 'category': 'code'},
        'npm': {'url': 'https://registry.npmjs.org/-/v1/search?text={query}&size=3', 'category': 'code'},
        'pypi': {'url': 'https://pypi.org/pypi/{query}/json', 'category': 'code'},
        'crates_io': {'url': 'https://crates.io/api/v1/crates?q={query}&per_page=3', 'category': 'code'},
        'packagist': {'url': 'https://packagist.org/search.json?q={query}&per_page=3', 'category': 'code'},
        'rubygems': {'url': 'https://rubygems.org/api/v1/search.json?query={query}', 'category': 'code'},
        'dockerhub': {'url': 'https://hub.docker.com/v2/search/repositories/?query={query}&page_size=3', 'category': 'code'},
        'gitlab': {'url': 'https://gitlab.com/api/v4/projects?search={query}&per_page=3', 'category': 'code'},
        'devdocs': {'url': 'https://devdocs.io/api/search?q={query}', 'category': 'code'},
        
        # ── LANGUAGE & DICTIONARY ──
        'dictionary': {'url': 'https://api.dictionaryapi.dev/api/v2/entries/en/{query}', 'category': 'language'},
        'datamuse': {'url': 'https://api.datamuse.com/words?ml={query}&max=5', 'category': 'language'},
        'urban_dictionary': {'url': 'https://api.urbandictionary.com/v0/define?term={query}', 'category': 'language'},
        'libre_translate': {'url': 'https://libretranslate.com/detect', 'category': 'language'},
        
        # ── NEWS & MEDIA ──
        'hackernews': {'url': 'https://hn.algolia.com/api/v1/search?query={query}&hitsPerPage=3', 'category': 'news'},
        'reddit': {'url': 'https://www.reddit.com/search.json?q={query}&limit=3&sort=relevance', 'category': 'social'},
        'lobsters': {'url': 'https://lobste.rs/search.json?q={query}', 'category': 'news'},
        'devto': {'url': 'https://dev.to/api/articles?tag={query}&per_page=3', 'category': 'news'},
        'newsapi_free': {'url': 'https://gnews.io/api/v4/search?q={query}&lang=en&max=3', 'category': 'news'},
        
        # ── BOOKS & LITERATURE ──
        'openlibrary': {'url': 'https://openlibrary.org/search.json?q={query}&limit=3', 'category': 'books'},
        'gutenberg': {'url': 'https://gutendex.com/books/?search={query}', 'category': 'books'},
        'google_books': {'url': 'https://www.googleapis.com/books/v1/volumes?q={query}&maxResults=3', 'category': 'books'},
        'libgen_search': {'url': 'https://openlibrary.org/search.json?title={query}&limit=3', 'category': 'books'},
        
        # ── GEOGRAPHY & MAPS ──
        'nominatim': {'url': 'https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=3', 'category': 'geography'},
        'restcountries': {'url': 'https://restcountries.com/v3.1/name/{query}', 'category': 'geography'},
        'geonames': {'url': 'http://api.geonames.org/searchJSON?q={query}&maxRows=3&username=demo', 'category': 'geography'},
        'openweather': {'url': 'https://api.openweathermap.org/data/2.5/weather?q={query}&appid=demo', 'category': 'weather'},
        
        # ── FINANCE & ECONOMY ──
        'coincap': {'url': 'https://api.coincap.io/v2/assets?search={query}&limit=3', 'category': 'finance'},
        'coingecko': {'url': 'https://api.coingecko.com/api/v3/search?query={query}', 'category': 'finance'},
        'exchangerate': {'url': 'https://open.er-api.com/v6/latest/{query}', 'category': 'finance'},
        'worldbank': {'url': 'https://api.worldbank.org/v2/country/{query}?format=json', 'category': 'finance'},
        
        # ── ENTERTAINMENT & MEDIA ──
        'itunes': {'url': 'https://itunes.apple.com/search?term={query}&limit=3', 'category': 'media'},
        'openmoviedb': {'url': 'https://www.omdbapi.com/?s={query}&type=movie', 'category': 'media'},
        'tvmaze': {'url': 'https://api.tvmaze.com/search/shows?q={query}', 'category': 'media'},
        'jikan_anime': {'url': 'https://api.jikan.moe/v4/anime?q={query}&limit=3', 'category': 'media'},
        'rawg_games': {'url': 'https://api.rawg.io/api/games?search={query}&page_size=3', 'category': 'media'},
        'musicbrainz': {'url': 'https://musicbrainz.org/ws/2/artist/?query={query}&fmt=json&limit=3', 'category': 'media'},
        'lyrics_ovh': {'url': 'https://api.lyrics.ovh/v1/{query}', 'category': 'media'},
        'pokapi': {'url': 'https://pokeapi.co/api/v2/pokemon/{query}', 'category': 'media'},
        
        # ── FOOD & HEALTH ──
        'edamam': {'url': 'https://api.edamam.com/search?q={query}&to=3', 'category': 'food'},
        'open_food_facts': {'url': 'https://world.openfoodfacts.org/cgi/search.pl?search_terms={query}&json=1&page_size=3', 'category': 'food'},
        'cocktaildb': {'url': 'https://www.thecocktaildb.com/api/json/v1/1/search.php?s={query}', 'category': 'food'},
        'mealdb': {'url': 'https://www.themealdb.com/api/json/v1/1/search.php?s={query}', 'category': 'food'},
        
        # ── GOVERNMENT & LAW ──
        'federalregister': {'url': 'https://www.federalregister.gov/api/v1/documents.json?conditions[term]={query}&per_page=3', 'category': 'government'},
        'congress_bills': {'url': 'https://api.congress.gov/v3/bill?query={query}', 'category': 'government'},
        'data_gov': {'url': 'https://catalog.data.gov/api/3/action/package_search?q={query}&rows=3', 'category': 'government'},
        'eu_data': {'url': 'https://data.europa.eu/api/hub/search/datasets?q={query}&limit=3', 'category': 'government'},
        
        # ── EDUCATION ──
        'ted_talks': {'url': 'https://api.ted.com/v1/search?q={query}', 'category': 'education'},
        'khan_academy': {'url': 'https://www.khanacademy.org/api/v2/search?query={query}', 'category': 'education'},
        'mit_ocw': {'url': 'https://ocw.mit.edu/search/?q={query}&type=course', 'category': 'education'},
        
        # ── ENVIRONMENT & NATURE ──
        'gbif': {'url': 'https://api.gbif.org/v1/species/search?q={query}&limit=3', 'category': 'nature'},
        'ebird': {'url': 'https://api.ebird.org/v2/ref/taxon/find?q={query}', 'category': 'nature'},
        'inaturalist': {'url': 'https://api.inaturalist.org/v1/taxa?q={query}&per_page=3', 'category': 'nature'},
        'earthquake': {'url': 'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&limit=3&orderby=time', 'category': 'nature'},
        
        # ── ART & CULTURE ──
        'met_museum': {'url': 'https://collectionapi.metmuseum.org/public/collection/v1/search?q={query}', 'category': 'art'},
        'rijksmuseum': {'url': 'https://www.rijksmuseum.nl/api/en/collection?q={query}&key=demo&format=json&ps=3', 'category': 'art'},
        'europeana': {'url': 'https://api.europeana.eu/record/v2/search.json?query={query}&rows=3', 'category': 'art'},
        'harvard_art': {'url': 'https://api.harvardartmuseums.org/object?q={query}&size=3', 'category': 'art'},
        'smithsonian': {'url': 'https://api.si.edu/openaccess/api/v1.0/search?q={query}&rows=3', 'category': 'art'},
        
        # ── MATH & COMPUTATION ──
        'mathjs': {'url': 'https://api.mathjs.org/v4/?expr={query}', 'category': 'math'},
        'numbersapi': {'url': 'http://numbersapi.com/{query}/math?json', 'category': 'math'},
        'wolframalpha_short': {'url': 'https://api.wolframalpha.com/v1/result?appid=DEMO&i={query}', 'category': 'math'},
        
        # ── SPACE & ASTRONOMY ──
        'nasa_apod': {'url': 'https://api.nasa.gov/planetary/apod?api_key=DEMO_KEY', 'category': 'space'},
        'spacex': {'url': 'https://api.spacexdata.com/v4/launches/latest', 'category': 'space'},
        'iss_location': {'url': 'http://api.open-notify.org/iss-now.json', 'category': 'space'},
        'solar_system': {'url': 'https://api.le-systeme-solaire.net/rest/bodies/{query}', 'category': 'space'},
        
        # ── SOCIAL & PEOPLE ──
        'randomuser': {'url': 'https://randomuser.me/api/', 'category': 'social'},
        'agify': {'url': 'https://api.agify.io?name={query}', 'category': 'social'},
        'genderize': {'url': 'https://api.genderize.io?name={query}', 'category': 'social'},
        'nationalize': {'url': 'https://api.nationalize.io?name={query}', 'category': 'social'},
        
        # ── UTILITIES ──
        'ipinfo': {'url': 'https://ipapi.co/{query}/json/', 'category': 'utility'},
        'qrcode': {'url': 'https://api.qrserver.com/v1/create-qr-code/?data={query}&size=200x200', 'category': 'utility'},
        'placeholder': {'url': 'https://jsonplaceholder.typicode.com/posts?_limit=3', 'category': 'utility'},
        'httpbin': {'url': 'https://httpbin.org/get', 'category': 'utility'},
        'uuid': {'url': 'https://www.uuidtools.com/api/generate/v4', 'category': 'utility'},
        
        # ── BIOLOGY & CHEMISTRY ──
        'uniprot': {'url': 'https://rest.uniprot.org/uniprotkb/search?query={query}&size=3&format=json', 'category': 'biology'},
        'chembl': {'url': 'https://www.ebi.ac.uk/chembl/api/data/molecule/search?q={query}&limit=3&format=json', 'category': 'chemistry'},
        'pubchem': {'url': 'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{query}/JSON', 'category': 'chemistry'},
        'ebi_proteins': {'url': 'https://www.ebi.ac.uk/proteins/api/proteins?offset=0&size=3&keyword={query}', 'category': 'biology'},
        
        # ── MORE APIs TO REACH 200+ ──
        'color_api': {'url': 'https://www.thecolorapi.com/id?hex={query}', 'category': 'utility'},
        'catfact': {'url': 'https://catfact.ninja/fact', 'category': 'fun'},
        'dogapi': {'url': 'https://dog.ceo/api/breeds/list/all', 'category': 'fun'},
        'advice': {'url': 'https://api.adviceslip.com/advice/search/{query}', 'category': 'fun'},
        'jokes': {'url': 'https://v2.jokeapi.dev/joke/Any?contains={query}', 'category': 'fun'},
        'trivia': {'url': 'https://opentdb.com/api.php?amount=3&category=9', 'category': 'fun'},
        'quotes': {'url': 'https://api.quotable.io/search/quotes?query={query}&limit=3', 'category': 'fun'},
        'zenquotes': {'url': 'https://zenquotes.io/api/random', 'category': 'fun'},
        'affirmations': {'url': 'https://www.affirmations.dev/', 'category': 'fun'},
        'bored_api': {'url': 'https://www.boredapi.com/api/activity', 'category': 'fun'},
        'chuck_norris': {'url': 'https://api.chucknorris.io/jokes/search?query={query}', 'category': 'fun'},
        'uselessfacts': {'url': 'https://uselessfacts.jsph.pl/api/v2/facts/search?query={query}', 'category': 'fun'},
        'crypto_news': {'url': 'https://min-api.cryptocompare.com/data/v2/news/?categories={query}', 'category': 'finance'},
        'carbon_intensity': {'url': 'https://api.carbonintensity.org.uk/intensity', 'category': 'environment'},
        'covid_data': {'url': 'https://disease.sh/v3/covid-19/countries/{query}', 'category': 'health'},
        'virus_total': {'url': 'https://www.virustotal.com/vtapi/v2/domain/report?domain={query}', 'category': 'security'},
        'shodan_host': {'url': 'https://api.shodan.io/dns/resolve?hostnames={query}', 'category': 'security'},
        'cve_search': {'url': 'https://cve.circl.lu/api/search/{query}', 'category': 'security'},
        'wayback': {'url': 'https://archive.org/wayback/available?url={query}', 'category': 'archive'},
        'archive_search': {'url': 'https://archive.org/advancedsearch.php?q={query}&output=json&rows=3', 'category': 'archive'},
        'giphy': {'url': 'https://api.giphy.com/v1/gifs/search?q={query}&api_key=dc6zaTOxFJmzC&limit=3', 'category': 'media'},
        'unsplash': {'url': 'https://api.unsplash.com/search/photos?query={query}&per_page=3', 'category': 'media'},
        'pexels': {'url': 'https://api.pexels.com/v1/search?query={query}&per_page=3', 'category': 'media'},
        'flickr': {'url': 'https://api.flickr.com/services/rest/?method=flickr.photos.search&text={query}&format=json&nojsoncallback=1&per_page=3', 'category': 'media'},
        'tmdb_movies': {'url': 'https://api.themoviedb.org/3/search/movie?query={query}', 'category': 'media'},
        'igdb': {'url': 'https://api.igdb.com/v4/games?search={query}', 'category': 'media'},
        'twitch': {'url': 'https://api.twitch.tv/helix/search/channels?query={query}', 'category': 'media'},
        'spotify_search': {'url': 'https://api.spotify.com/v1/search?q={query}&type=track&limit=3', 'category': 'media'},
        'lastfm': {'url': 'https://ws.audioscrobbler.com/2.0/?method=artist.search&artist={query}&api_key=demo&format=json&limit=3', 'category': 'media'},
        'eventbrite': {'url': 'https://www.eventbriteapi.com/v3/events/search/?q={query}', 'category': 'events'},
        'meetup': {'url': 'https://api.meetup.com/find/upcoming_events?text={query}', 'category': 'events'},
        'openaq': {'url': 'https://api.openaq.org/v2/measurements?city={query}&limit=3', 'category': 'environment'},
        'weather_gov': {'url': 'https://api.weather.gov/points/{query}', 'category': 'weather'},
        'sunrise_sunset': {'url': 'https://api.sunrise-sunset.org/json?lat=36.7201600&lng=-4.4203400&formatted=0', 'category': 'weather'},
        'timezone': {'url': 'http://worldtimeapi.org/api/timezone/{query}', 'category': 'utility'},
        'country_is': {'url': 'https://api.country.is/{query}', 'category': 'utility'},
        'iana_tld': {'url': 'https://data.iana.org/TLD/tlds-alpha-by-domain.txt', 'category': 'utility'},
        'emoji_api': {'url': 'https://emoji-api.com/emojis?search={query}&access_key=demo', 'category': 'utility'},
        'favicon': {'url': 'https://favicone.com/{query}', 'category': 'utility'},
        'ssl_labs': {'url': 'https://api.ssllabs.com/api/v3/analyze?host={query}', 'category': 'security'},
        'whoami': {'url': 'https://httpbin.org/ip', 'category': 'utility'},
        'headers': {'url': 'https://httpbin.org/headers', 'category': 'utility'},
        'postman_echo': {'url': 'https://postman-echo.com/get?query={query}', 'category': 'utility'},
        'jsontest': {'url': 'http://ip.jsontest.com/', 'category': 'utility'},
        'wttr_weather': {'url': 'https://wttr.in/{query}?format=j1', 'category': 'weather'},
        'metaweather': {'url': 'https://www.metaweather.com/api/location/search/?query={query}', 'category': 'weather'},
        'openmeteo': {'url': 'https://geocoding-api.open-meteo.com/v1/search?name={query}&count=3', 'category': 'weather'},
        'ip_api': {'url': 'http://ip-api.com/json/{query}', 'category': 'utility'},
        'abstract_holidays': {'url': 'https://date.nager.at/api/v3/publicholidays/2024/{query}', 'category': 'utility'},
        'calendarific': {'url': 'https://calendarific.com/api/v2/holidays?api_key=demo&country={query}&year=2024', 'category': 'utility'},
        'recipe_puppy': {'url': 'http://www.recipepuppy.com/api/?q={query}', 'category': 'food'},
        'spoonacular': {'url': 'https://api.spoonacular.com/recipes/complexSearch?query={query}&number=3', 'category': 'food'},
        'nutritionix': {'url': 'https://trackapi.nutritionix.com/v2/search/instant?query={query}', 'category': 'food'},
        'cat_breeds': {'url': 'https://api.thecatapi.com/v1/breeds/search?q={query}', 'category': 'nature'},
        'dog_breeds': {'url': 'https://api.thedogapi.com/v1/breeds/search?q={query}', 'category': 'nature'},
        'fish_base': {'url': 'https://fishbase.ropensci.org/species?genus={query}', 'category': 'nature'},
        'plant_id': {'url': 'https://trefle.io/api/v1/plants/search?q={query}', 'category': 'nature'},
        'mineral_db': {'url': 'https://mindat.org/api/search?q={query}', 'category': 'science'},
        'osm_search': {'url': 'https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=3', 'category': 'geography'},
        'mapquest': {'url': 'https://www.mapquestapi.com/geocoding/v1/address?key=demo&location={query}', 'category': 'geography'},
        'postcodes': {'url': 'https://api.postcodes.io/postcodes/{query}', 'category': 'geography'},
        'zippopotam': {'url': 'https://api.zippopotam.us/us/{query}', 'category': 'geography'},
        'census_gov': {'url': 'https://api.census.gov/data/2021/pep/population?get=NAME,POP_2021&for=state:{query}', 'category': 'government'},
        'fda_drugs': {'url': 'https://api.fda.gov/drug/label.json?search={query}&limit=3', 'category': 'health'},
        'clinicaltrials': {'url': 'https://clinicaltrials.gov/api/v2/studies?query.term={query}&pageSize=3', 'category': 'health'},
        'disease_ontology': {'url': 'https://www.disease-ontology.org/api/metadata/{query}', 'category': 'health'},
        'rxnorm': {'url': 'https://rxnav.nlm.nih.gov/REST/drugs.json?name={query}', 'category': 'health'},
        'fhir_server': {'url': 'https://hapi.fhir.org/baseR4/Patient?name={query}&_count=3&_format=json', 'category': 'health'},
        'mesh_nlm': {'url': 'https://id.nlm.nih.gov/mesh/lookup/descriptor?label={query}&match=contains&limit=3', 'category': 'health'},
        'open_targets': {'url': 'https://api.opentargets.io/v3/platform/public/search?q={query}&size=3', 'category': 'health'},
        'human_protein': {'url': 'https://www.proteinatlas.org/api/search_download.php?search={query}&format=json', 'category': 'biology'},
        'ensembl': {'url': 'https://rest.ensembl.org/lookup/symbol/homo_sapiens/{query}?content-type=application/json', 'category': 'biology'},
        'kegg': {'url': 'https://rest.kegg.jp/find/compound/{query}', 'category': 'biology'},
        'interpro': {'url': 'https://www.ebi.ac.uk/interpro/api/entry/interpro?search={query}&page_size=3', 'category': 'biology'},
        'reactome': {'url': 'https://reactome.org/ContentService/search/query?query={query}&types=Pathway&cluster=true', 'category': 'biology'},
        'string_db': {'url': 'https://string-db.org/api/json/resolve?identifier={query}&species=9606', 'category': 'biology'},
        'ols_ontology': {'url': 'https://www.ebi.ac.uk/ols/api/search?q={query}&rows=3', 'category': 'science'},
        'materials_project': {'url': 'https://api.materialsproject.org/materials/search?keywords={query}', 'category': 'science'},
        'nist_constants': {'url': 'https://physics.nist.gov/cgi-bin/cuu/Value?{query}', 'category': 'science'},
        'periodic_table': {'url': 'https://neelpatel05.pythonanywhere.com/element/atomicnumber/{query}', 'category': 'science'},
        'spaceflight_news': {'url': 'https://api.spaceflightnewsapi.net/v4/articles/?search={query}&limit=3', 'category': 'space'},
        'launch_library': {'url': 'https://ll.thespacedevs.com/2.2.0/launch/?search={query}&limit=3', 'category': 'space'},
        'exoplanets': {'url': 'https://exoplanetarchive.ipac.caltech.edu/cgi-bin/nstedAPI/nph-nstedAPI?table=exoplanets&format=json&where=pl_name like %27%25{query}%25%27', 'category': 'space'},
        'asteroids': {'url': 'https://api.nasa.gov/neo/rest/v1/neo/browse?api_key=DEMO_KEY', 'category': 'space'},
        'mars_weather': {'url': 'https://api.nasa.gov/insight_weather/?api_key=DEMO_KEY&feedtype=json&ver=1.0', 'category': 'space'},
        'github_gists': {'url': 'https://api.github.com/gists/public?per_page=3', 'category': 'code'},
        'cdnjs': {'url': 'https://api.cdnjs.com/libraries?search={query}&limit=3', 'category': 'code'},
        'bundlephobia': {'url': 'https://bundlephobia.com/api/size?package={query}', 'category': 'code'},
        'shields_io': {'url': 'https://img.shields.io/badge/{query}-blue', 'category': 'code'},
        'haveibeenpwned': {'url': 'https://haveibeenpwned.com/api/v3/breachedaccount/{query}', 'category': 'security'},
        'abuse_ipdb': {'url': 'https://api.abuseipdb.com/api/v2/check?ipAddress={query}', 'category': 'security'},
        'phishtank': {'url': 'https://checkurl.phishtank.com/checkurl/', 'category': 'security'},
        'virustotal_url': {'url': 'https://www.virustotal.com/vtapi/v2/url/report?resource={query}', 'category': 'security'},
        'blockchain_info': {'url': 'https://blockchain.info/rawaddr/{query}', 'category': 'crypto'},
        'etherscan': {'url': 'https://api.etherscan.io/api?module=account&action=balance&address={query}', 'category': 'crypto'},
        'coinmarketcap': {'url': 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?symbol={query}', 'category': 'crypto'},
        'binance': {'url': 'https://api.binance.com/api/v3/ticker/price?symbol={query}USDT', 'category': 'crypto'},
        'fixer': {'url': 'https://data.fixer.io/api/latest?base={query}', 'category': 'finance'},
        'alpha_vantage': {'url': 'https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={query}&apikey=demo', 'category': 'finance'},
        'iex_cloud': {'url': 'https://cloud.iexapis.com/stable/stock/{query}/quote', 'category': 'finance'},
        'open_exchange': {'url': 'https://openexchangerates.org/api/latest.json?base={query}', 'category': 'finance'},
        'football_data': {'url': 'https://api.football-data.org/v4/competitions?areas={query}', 'category': 'sports'},
        'nba_data': {'url': 'https://www.balldontlie.io/api/v1/players?search={query}&per_page=3', 'category': 'sports'},
        'f1_data': {'url': 'https://ergast.com/api/f1/drivers/{query}.json', 'category': 'sports'},
        'cricket_api': {'url': 'https://cricbuzz-cricket.p.rapidapi.com/stats/v1/player/search?plrN={query}', 'category': 'sports'},
    }
    
    @staticmethod
    def determine_relevant_sources(query):
        """Smart source selection based on query content"""
        query_lower = query.lower()
        selected = set()
        
        keyword_map = {
            'code': ['github_repos', 'stackoverflow', 'npm', 'pypi', 'devto', 'github_code', 'cdnjs'],
            'python': ['pypi', 'stackoverflow', 'github_repos', 'devto', 'github_code'],
            'javascript': ['npm', 'stackoverflow', 'github_repos', 'cdnjs', 'bundlephobia'],
            'program': ['github_repos', 'stackoverflow', 'devto', 'github_code'],
            'api': ['github_repos', 'stackoverflow', 'devto', 'npm'],
            'science': ['arxiv', 'semanticscholar', 'openalex', 'crossref', 'europe_pmc'],
            'research': ['arxiv', 'semanticscholar', 'crossref', 'openalex', 'doaj'],
            'paper': ['arxiv', 'semanticscholar', 'crossref', 'europe_pmc', 'doaj'],
            'medical': ['pubmed', 'clinicaltrials', 'fda_drugs', 'europe_pmc', 'rxnorm'],
            'health': ['pubmed', 'clinicaltrials', 'fda_drugs', 'open_targets', 'disease_ontology'],
            'drug': ['fda_drugs', 'pubmed', 'rxnorm', 'chembl', 'pubchem'],
            'disease': ['pubmed', 'disease_ontology', 'clinicaltrials', 'open_targets'],
            'protein': ['uniprot', 'human_protein', 'ebi_proteins', 'string_db', 'interpro'],
            'gene': ['ensembl', 'uniprot', 'string_db', 'reactome'],
            'chemistry': ['pubchem', 'chembl', 'periodic_table'],
            'biology': ['uniprot', 'gbif', 'inaturalist', 'ensembl', 'kegg'],
            'news': ['hackernews', 'reddit', 'devto', 'lobsters'],
            'weather': ['wttr_weather', 'openmeteo'],
            'book': ['openlibrary', 'gutenberg', 'google_books'],
            'movie': ['tvmaze', 'jikan_anime'],
            'music': ['itunes', 'musicbrainz'],
            'game': ['rawg_games'],
            'anime': ['jikan_anime'],
            'food': ['open_food_facts', 'mealdb', 'cocktaildb'],
            'recipe': ['mealdb', 'open_food_facts'],
            'country': ['restcountries', 'worldbank'],
            'geography': ['nominatim', 'restcountries', 'openmeteo'],
            'location': ['nominatim', 'osm_search', 'zippopotam'],
            'map': ['nominatim', 'osm_search'],
            'crypto': ['coincap', 'coingecko', 'binance', 'blockchain_info'],
            'bitcoin': ['coincap', 'coingecko', 'binance', 'crypto_news'],
            'stock': ['alpha_vantage'],
            'finance': ['coincap', 'exchangerate', 'worldbank', 'alpha_vantage'],
            'currency': ['exchangerate', 'open_exchange'],
            'space': ['spaceflight_news', 'launch_library', 'nasa_apod', 'spacex', 'solar_system'],
            'nasa': ['nasa_images', 'nasa_apod', 'nasa_patents', 'spaceflight_news'],
            'planet': ['solar_system', 'exoplanets'],
            'star': ['solar_system', 'spaceflight_news'],
            'math': ['mathjs', 'numbersapi'],
            'calculate': ['mathjs'],
            'definition': ['dictionary', 'datamuse'],
            'meaning': ['dictionary', 'urban_dictionary'],
            'word': ['dictionary', 'datamuse'],
            'translate': ['libre_translate', 'dictionary'],
            'animal': ['gbif', 'inaturalist', 'cat_breeds', 'dog_breeds'],
            'plant': ['gbif', 'inaturalist'],
            'art': ['met_museum', 'rijksmuseum', 'europeana', 'smithsonian'],
            'museum': ['met_museum', 'rijksmuseum', 'harvard_art', 'smithsonian'],
            'security': ['cve_search', 'ssl_labs'],
            'hack': ['cve_search', 'stackoverflow'],
            'ip': ['ipinfo', 'ip_api'],
            'sport': ['football_data', 'nba_data', 'f1_data'],
            'football': ['football_data'],
            'cricket': ['cricket_api'],
            'government': ['data_gov', 'federalregister'],
            'law': ['federalregister', 'congress_bills'],
            'earthquake': ['earthquake'],
            'covid': ['covid_data'],
            'joke': ['jokes', 'chuck_norris'],
            'quote': ['quotes', 'zenquotes'],
            'fun': ['jokes', 'trivia', 'advice', 'catfact'],
            'docker': ['dockerhub', 'github_repos'],
            'linux': ['stackexchange', 'stackoverflow', 'github_repos'],
        }
        
        for keyword, sources in keyword_map.items():
            if keyword in query_lower:
                selected.update(sources)
        
        # Always include general knowledge
        selected.update(['wikipedia_search', 'duckduckgo'])
        
        # Cap at 8 sources for speed
        return list(selected)[:8]
    
    @staticmethod
    def fetch_source(source_name, query, timeout=4):
        """Fetch data from a single source"""
        try:
            source = KnowledgeEngine.SOURCES.get(source_name)
            if not source:
                return None
            
            encoded_query = quote_plus(query)
            url = source['url'].format(query=encoded_query)
            
            headers = {
                'User-Agent': 'NexusAI-Pro/2.0 (Research Bot)',
                'Accept': 'application/json'
            }
            
            response = requests.get(url, headers=headers, timeout=timeout)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    return {
                        'source': source_name,
                        'category': source['category'],
                        'data': data,
                        'status': 'success'
                    }
                except Exception:
                    if len(response.text) < 5000:
                        return {
                            'source': source_name,
                            'category': source['category'],
                            'data': response.text[:2000],
                            'status': 'success'
                        }
        except Exception:
            pass
        return None
    
    @staticmethod
    def search(query, max_sources=8):
        """Search relevant sources concurrently"""
        relevant_sources = KnowledgeEngine.determine_relevant_sources(query)[:max_sources]
        results = []
        
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(KnowledgeEngine.fetch_source, src, query): src 
                for src in relevant_sources
            }
            for future in concurrent.futures.as_completed(futures, timeout=6):
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception:
                    pass
        
        return results
    
    @staticmethod
    def format_context(results, max_length=4000):
        """Format search results into context for the LLM"""
        if not results:
            return ""
        
        context_parts = []
        total_len = 0
        
        for r in results:
            data = r.get('data', '')
            source = r.get('source', 'unknown')
            
            if isinstance(data, dict):
                # Extract relevant text from common API response formats
                text_parts = []
                for key in ['extract', 'summary', 'description', 'abstract', 'content', 
                           'title', 'name', 'text', 'definition', 'answer',
                           'snippet', 'body', 'message', 'explanation']:
                    if key in data and data[key]:
                        text_parts.append(str(data[key])[:500])
                
                # Handle nested structures
                for key in ['query', 'results', 'items', 'data', 'hits', 'records',
                           'documents', 'entries', 'works', 'articles', 'list']:
                    if key in data and isinstance(data[key], (list, dict)):
                        nested = data[key]
                        if isinstance(nested, list):
                            for item in nested[:2]:
                                if isinstance(item, dict):
                                    for subkey in ['title', 'name', 'description', 'summary', 
                                                  'extract', 'snippet', 'text', 'abstract',
                                                  'content', 'body']:
                                        if subkey in item and item[subkey]:
                                            text_parts.append(str(item[subkey])[:300])
                                elif isinstance(item, str):
                                    text_parts.append(item[:300])
                        elif isinstance(nested, dict):
                            for subkey in ['title', 'description', 'summary', 'text']:
                                if subkey in nested and nested[subkey]:
                                    text_parts.append(str(nested[subkey])[:300])
                
                formatted = ' | '.join(text_parts[:3])
            elif isinstance(data, list):
                parts = []
                for item in data[:2]:
                    if isinstance(item, dict):
                        for key in ['title', 'name', 'description', 'summary', 'text']:
                            if key in item and item[key]:
                                parts.append(str(item[key])[:300])
                                break
                    else:
                        parts.append(str(item)[:200])
                formatted = ' | '.join(parts)
            else:
                formatted = str(data)[:500]
            
            if formatted and len(formatted.strip()) > 10:
                entry = f"[{source}]: {formatted.strip()}"
                if total_len + len(entry) > max_length:
                    break
                context_parts.append(entry)
                total_len += len(entry)
        
        return '\n'.join(context_parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GROQ LLM ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LLMEngine:
    MODELS = [
        'llama-3.3-70b-versatile',
        'llama-3.1-70b-versatile',
        'llama-3.1-8b-instant',
        'llama3-70b-8192',
        'llama3-8b-8192',
        'mixtral-8x7b-32768',
        'gemma2-9b-it',
        'gemma-7b-it',
    ]
    
    @staticmethod
    def get_client():
        api_key = get_api_key('groq')
        if not api_key:
            api_key = os.environ.get('GROQ_API_KEY', '')
        if not api_key:
            return None
        return Groq(api_key=api_key)
    
    @staticmethod
    def get_model():
        model = get_setting('default_model', 'llama-3.3-70b-versatile')
        return model if model else 'llama-3.3-70b-versatile'
    
    @staticmethod
    def get_system_prompt():
        site_name = get_setting('site_name', 'NexusAI Pro')
        custom_instructions = get_setting('system_instructions', '')
        
        base_prompt = f"""You are {site_name}, an advanced AI assistant with access to real-time knowledge from hundreds of open-source databases. You are helpful, accurate, concise, and friendly.

Core Rules:
1. Use the provided context/knowledge to give accurate, up-to-date answers
2. If context is provided, prioritize it for factual accuracy
3. NEVER mention or list the databases/sources used unless the user specifically asks "what sources did you use"
4. Be conversational yet precise
5. Use markdown formatting for better readability when appropriate
6. If you don't know something and no context helps, say so honestly
7. For code questions, provide clean, well-commented code
8. Be concise - don't over-explain unless asked"""
        
        if custom_instructions:
            base_prompt += f"\n\nAdditional Instructions from Admin:\n{custom_instructions}"
        
        return base_prompt
    
    @staticmethod
    def chat(messages, stream=False):
        client = LLMEngine.get_client()
        if not client:
            return "⚠️ No API key configured. Please ask the admin to set up the Groq API key."
        
        model = LLMEngine.get_model()
        
        try:
            if stream:
                return client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=float(get_setting('temperature', '0.7')),
                    max_tokens=int(get_setting('max_tokens', '4096')),
                    top_p=float(get_setting('top_p', '0.9')),
                    stream=True
                )
            else:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=float(get_setting('temperature', '0.7')),
                    max_tokens=int(get_setting('max_tokens', '4096')),
                    top_p=float(get_setting('top_p', '0.9')),
                    stream=False
                )
                return response.choices[0].message.content
        except Exception as e:
            logger.error(f"LLM Error: {e}")
            return f"⚠️ AI Engine Error: {str(e)}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INITIALIZE DATABASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def init_database():
    """Initialize database with default settings and admin user"""
    db.create_all()
    
    # Default admin
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(
            username='admin',
            email='admin@nexusai.pro',
            is_admin=True,
            is_active_user=True
        )
        admin.set_password('admin123')  # Change this!
        db.session.add(admin)
    
    # Default settings
    defaults = {
        'site_name': 'NexusAI Pro',
        'site_tagline': 'God-Level AI Assistant',
        'site_logo_emoji': '🧠',
        'site_logo_url': '',
        'primary_color': '#8B5CF6',
        'secondary_color': '#06B6D4',
        'accent_color': '#F59E0B',
        'background_video_url': '',
        'login_audio_url': '',
        'enable_login_audio': 'false',
        'default_model': 'llama-3.3-70b-versatile',
        'temperature': '0.7',
        'max_tokens': '4096',
        'top_p': '0.9',
        'system_instructions': '',
        'enable_rag': 'true',
        'max_rag_sources': '8',
        'enable_registration': 'true',
        'maintenance_mode': 'false',
        'maintenance_message': 'We are currently undergoing maintenance. Please check back soon.',
        'welcome_message': 'Hello! I\'m your AI assistant. How can I help you today?',
        'enable_analytics': 'true',
        'rate_limit_per_minute': '30',
        'max_message_length': '10000',
        'enable_markdown': 'true',
        'enable_code_highlight': 'true',
        'custom_css': '',
        'custom_js': '',
        'footer_text': 'Powered by NexusAI Pro',
        'meta_description': 'Advanced AI Assistant powered by 200+ knowledge sources',
        'enable_announcements': 'true',
        'chat_placeholder': 'Ask me anything...',
        'shutdown_mode': 'false',
    }
    
    for key, value in defaults.items():
        if not SiteSettings.query.filter_by(key=key).first():
            db.session.add(SiteSettings(key=key, value=value))
    
    # Default Groq API key from env
    groq_key = os.environ.get('GROQ_API_KEY', '')
    if groq_key and not APIKey.query.filter_by(service_name='groq').first():
        db.session.add(APIKey(service_name='groq', api_key=groq_key, is_active=True))
    
    db.session.commit()
    logger.info("✅ Database initialized successfully")

with app.app_context():
    init_database()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES - AUTH
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.before_request
def check_maintenance():
    if get_setting('shutdown_mode', 'false') == 'true':
        if request.endpoint not in ['login_page', 'do_login', 'admin_panel', 'admin_api', 'static']:
            if not (current_user.is_authenticated and current_user.is_admin):
                return jsonify({'error': 'System is shut down by admin'}), 503
    
    if get_setting('maintenance_mode', 'false') == 'true':
        if request.endpoint not in ['login_page', 'do_login', 'admin_panel', 'admin_api', 'static']:
            if not (current_user.is_authenticated and current_user.is_admin):
                msg = get_setting('maintenance_message', 'Under maintenance')
                return f'<html><body style="background:#0a0a0a;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif"><div style="text-align:center"><h1>🔧 Maintenance Mode</h1><p>{msg}</p></div></body></html>', 503


@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('chat_page'))
    return redirect(url_for('login_page'))


@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('chat_page'))
    return render_template('index.html', settings={
        'site_name': get_setting('site_name', 'NexusAI Pro'),
        'site_tagline': get_setting('site_tagline', 'God-Level AI Assistant'),
        'site_logo_emoji': get_setting('site_logo_emoji', '🧠'),
        'primary_color': get_setting('primary_color', '#8B5CF6'),
        'enable_registration': get_setting('enable_registration', 'true'),
        'login_audio_url': get_setting('login_audio_url', ''),
        'enable_login_audio': get_setting('enable_login_audio', 'false'),
        'background_video_url': get_setting('background_video_url', ''),
    })


@app.route('/api/auth/login', methods=['POST'])
def do_login():
    data = request.json
    username = bleach.clean(data.get('username', '').strip())
    password = data.get('password', '')
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    
    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        log_event('login_failed', {'username': username})
        return jsonify({'error': 'Invalid credentials'}), 401
    
    if user.is_banned:
        return jsonify({'error': 'Account is banned. Contact admin.'}), 403
    
    if not user.is_active_user:
        return jsonify({'error': 'Account is deactivated'}), 403
    
    user.last_login = datetime.utcnow()
    db.session.commit()
    
    login_user(user, remember=True)
    log_event('login_success', {'username': username}, user.id)
    
    return jsonify({
        'success': True,
        'user': {
            'id': user.id,
            'username': user.username,
            'is_admin': user.is_admin
        },
        'play_audio': get_setting('enable_login_audio', 'false') == 'true',
        'audio_url': get_setting('login_audio_url', '')
    })


@app.route('/api/auth/register', methods=['POST'])
def do_register():
    if get_setting('enable_registration', 'true') != 'true':
        return jsonify({'error': 'Registration is disabled'}), 403
    
    data = request.json
    username = bleach.clean(data.get('username', '').strip())
    password = data.get('password', '')
    email = bleach.clean(data.get('email', '').strip())
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    
    if len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters'}), 400
    
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already taken'}), 409
    
    user = User(username=username, email=email if email else None)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    
    login_user(user, remember=True)
    log_event('registration', {'username': username}, user.id)
    
    return jsonify({
        'success': True,
        'user': {
            'id': user.id,
            'username': user.username,
            'is_admin': user.is_admin
        },
        'play_audio': get_setting('enable_login_audio', 'false') == 'true',
        'audio_url': get_setting('login_audio_url', '')
    })


@app.route('/api/auth/logout', methods=['POST'])
@login_required
def do_logout():
    log_event('logout', {'username': current_user.username}, current_user.id)
    logout_user()
    return jsonify({'success': True})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES - CHAT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route('/chat')
@login_required
def chat_page():
    announcements = []
    if get_setting('enable_announcements', 'true') == 'true':
        announcements = Announcement.query.filter_by(is_active=True).order_by(
            desc(Announcement.created_at)).limit(5).all()
    
    return render_template('chat.html', settings={
        'site_name': get_setting('site_name', 'NexusAI Pro'),
        'site_logo_emoji': get_setting('site_logo_emoji', '🧠'),
        'primary_color': get_setting('primary_color', '#8B5CF6'),
        'secondary_color': get_setting('secondary_color', '#06B6D4'),
        'accent_color': get_setting('accent_color', '#F59E0B'),
        'welcome_message': get_setting('welcome_message', 'Hello! How can I help you today?'),
        'chat_placeholder': get_setting('chat_placeholder', 'Ask me anything...'),
        'enable_markdown': get_setting('enable_markdown', 'true'),
        'custom_css': get_setting('custom_css', ''),
        'footer_text': get_setting('footer_text', 'Powered by NexusAI Pro'),
        'user': {
            'id': current_user.id,
            'username': current_user.username,
            'is_admin': current_user.is_admin
        },
        'announcements': [{'title': a.title, 'content': a.content, 'priority': a.priority} for a in announcements]
    })


@app.route('/api/chats', methods=['GET'])
@login_required
def get_chats():
    chats = Chat.query.filter_by(
        user_id=current_user.id, is_archived=False
    ).order_by(desc(Chat.updated_at)).all()
    
    return jsonify([{
        'id': c.id,
        'title': c.title,
        'created_at': c.created_at.isoformat(),
        'updated_at': c.updated_at.isoformat(),
        'is_pinned': c.is_pinned,
        'message_count': c.messages.count()
    } for c in chats])


@app.route('/api/chats', methods=['POST'])
@login_required
def create_chat():
    chat = Chat(user_id=current_user.id, title='New Chat')
    db.session.add(chat)
    db.session.commit()
    
    return jsonify({
        'id': chat.id,
        'title': chat.title,
        'created_at': chat.created_at.isoformat()
    })


@app.route('/api/chats/<int:chat_id>', methods=['DELETE'])
@login_required
def delete_chat(chat_id):
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
    db.session.delete(chat)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/chats/<int:chat_id>/rename', methods=['PUT'])
@login_required
def rename_chat(chat_id):
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
    data = request.json
    chat.title = bleach.clean(data.get('title', 'Chat'))[:200]
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/chats/<int:chat_id>/pin', methods=['PUT'])
@login_required
def pin_chat(chat_id):
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
    chat.is_pinned = not chat.is_pinned
    db.session.commit()
    return jsonify({'success': True, 'is_pinned': chat.is_pinned})


@app.route('/api/chats/<int:chat_id>/messages', methods=['GET'])
@login_required
def get_messages(chat_id):
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
    
    messages = Message.query.filter_by(chat_id=chat_id).order_by(Message.created_at).all()
    
    return jsonify([{
        'id': m.id,
        'role': m.role,
        'content': m.content,
        'model_used': m.model_used,
        'created_at': m.created_at.isoformat(),
        'reaction': m.reaction
    } for m in messages])


@app.route('/api/chats/<int:chat_id>/send', methods=['POST'])
@login_required
def send_message(chat_id):
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
    
    data = request.json
    user_message = data.get('message', '').strip()
    
    if not user_message:
        return jsonify({'error': 'Message is empty'}), 400
    
    max_len = int(get_setting('max_message_length', '10000'))
    if len(user_message) > max_len:
        return jsonify({'error': f'Message too long (max {max_len} chars)'}), 400
    
    # Save user message
    user_msg = Message(chat_id=chat_id, role='user', content=user_message)
    db.session.add(user_msg)
    
    # Update chat title if first message
    if chat.messages.count() <= 1:
        chat.title = user_message[:100]
    chat.updated_at = datetime.utcnow()
    db.session.commit()
    
    # RAG - Search knowledge bases
    context = ""
    sources_used = []
    if get_setting('enable_rag', 'true') == 'true':
        try:
            max_sources = int(get_setting('max_rag_sources', '8'))
            results = KnowledgeEngine.search(user_message, max_sources=max_sources)
            context = KnowledgeEngine.format_context(results)
            sources_used = [r['source'] for r in results]
        except Exception as e:
            logger.error(f"RAG Error: {e}")
    
    # Build conversation history
    history = Message.query.filter_by(chat_id=chat_id).order_by(
        Message.created_at).limit(20).all()
    
    messages = [{"role": "system", "content": LLMEngine.get_system_prompt()}]
    
    if context:
        messages.append({
            "role": "system",
            "content": f"Real-time knowledge context (use this to answer accurately, do NOT mention these sources unless asked):\n{context}"
        })
    
    for msg in history:
        if msg.role in ('user', 'assistant'):
            messages.append({"role": msg.role, "content": msg.content})
    
    # Get AI response
    response = LLMEngine.chat(messages, stream=False)
    
    # Save assistant message
    assistant_msg = Message(
        chat_id=chat_id,
        role='assistant',
        content=response,
        model_used=LLMEngine.get_model(),
        sources_used=json.dumps(sources_used)
    )
    db.session.add(assistant_msg)
    
    # Update stats
    current_user.total_messages += 1
    db.session.commit()
    
    log_event('message_sent', {'chat_id': chat_id}, current_user.id)
    
    return jsonify({
        'id': assistant_msg.id,
        'role': 'assistant',
        'content': response,
        'model_used': assistant_msg.model_used,
        'created_at': assistant_msg.created_at.isoformat()
    })


@app.route('/api/chats/<int:chat_id>/stream', methods=['POST'])
@login_required
def stream_message(chat_id):
    """Streaming response endpoint"""
    chat = Chat.query.filter_by(id=chat_id, user_id=current_user.id).first()
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
    
    data = request.json
    user_message = data.get('message', '').strip()
    
    if not user_message:
        return jsonify({'error': 'Message is empty'}), 400
    
    # Save user message
    user_msg = Message(chat_id=chat_id, role='user', content=user_message)
    db.session.add(user_msg)
    
    if chat.messages.count() <= 1:
        chat.title = user_message[:100]
    chat.updated_at = datetime.utcnow()
    db.session.commit()
    
    # RAG
    context = ""
    sources_used = []
    if get_setting('enable_rag', 'true') == 'true':
        try:
            results = KnowledgeEngine.search(user_message, max_sources=int(get_setting('max_rag_sources', '8')))
            context = KnowledgeEngine.format_context(results)
            sources_used = [r['source'] for r in results]
        except Exception as e:
            logger.error(f"RAG Error: {e}")
    
    history = Message.query.filter_by(chat_id=chat_id).order_by(Message.created_at).limit(20).all()
    
    messages = [{"role": "system", "content": LLMEngine.get_system_prompt()}]
    
    if context:
        messages.append({
            "role": "system",
            "content": f"Real-time knowledge context:\n{context}"
        })
    
    for msg in history:
        if msg.role in ('user', 'assistant'):
            messages.append({"role": msg.role, "content": msg.content})
    
    def generate():
        full_response = ""
        try:
            stream = LLMEngine.chat(messages, stream=True)
            if isinstance(stream, str):
                full_response = stream
                yield f"data: {json.dumps({'content': stream, 'done': True})}\n\n"
            else:
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        yield f"data: {json.dumps({'content': content, 'done': False})}\n\n"
                yield f"data: {json.dumps({'content': '', 'done': True})}\n\n"
        except Exception as e:
            error_msg = f"⚠️ Streaming error: {str(e)}"
            full_response = error_msg
            yield f"data: {json.dumps({'content': error_msg, 'done': True})}\n\n"
        
        # Save the complete response
        try:
            with app.app_context():
                assistant_msg = Message(
                    chat_id=chat_id,
                    role='assistant',
                    content=full_response,
                    model_used=LLMEngine.get_model(),
                    sources_used=json.dumps(sources_used)
                )
                db.session.add(assistant_msg)
                user = db.session.get(User, current_user.id)
                if user:
                    user.total_messages += 1
                db.session.commit()
        except Exception:
            db.session.rollback()
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


@app.route('/api/messages/<int:msg_id>/react', methods=['PUT'])
@login_required
def react_message(msg_id):
    msg = Message.query.get(msg_id)
    if not msg:
        return jsonify({'error': 'Message not found'}), 404
    data = request.json
    msg.reaction = data.get('reaction', '')
    db.session.commit()
    return jsonify({'success': True})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES - ADMIN PANEL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    return render_template('admin.html', settings={
        'site_name': get_setting('site_name', 'NexusAI Pro'),
        'primary_color': get_setting('primary_color', '#8B5CF6'),
    })


@app.route('/api/admin/stats', methods=['GET'])
@login_required
@admin_required
def admin_stats():
    total_users = User.query.count()
    active_users = User.query.filter_by(is_active_user=True, is_banned=False).count()
    total_chats = Chat.query.count()
    total_messages = Message.query.count()
    
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_messages = Message.query.filter(Message.created_at >= today).count()
    today_users = User.query.filter(User.last_login >= today).count()
    today_new_users = User.query.filter(User.created_at >= today).count()
    
    week_ago = datetime.utcnow() - timedelta(days=7)
    week_messages = Message.query.filter(Message.created_at >= week_ago).count()
    
    # Message trends (last 7 days)
    trends = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        next_day = day + timedelta(days=1)
        count = Message.query.filter(
            Message.created_at >= day,
            Message.created_at < next_day
        ).count()
        trends.append({'date': day.strftime('%m/%d'), 'count': count})
    
    # Top users
    top_users = db.session.query(
        User.username, User.total_messages
    ).order_by(desc(User.total_messages)).limit(10).all()
    
    # Recent logs
    recent_logs = SystemLog.query.order_by(desc(SystemLog.created_at)).limit(20).all()
    
    return jsonify({
        'total_users': total_users,
        'active_users': active_users,
        'total_chats': total_chats,
        'total_messages': total_messages,
        'today_messages': today_messages,
        'today_active_users': today_users,
        'today_new_users': today_new_users,
        'week_messages': week_messages,
        'trends': trends,
        'top_users': [{'username': u[0], 'messages': u[1]} for u in top_users],
        'recent_logs': [{'level': l.level, 'message': l.message, 'source': l.source, 'time': l.created_at.isoformat()} for l in recent_logs],
        'total_sources': len(KnowledgeEngine.SOURCES),
        'model': LLMEngine.get_model()
    })


@app.route('/api/admin/settings', methods=['GET'])
@login_required
@admin_required
def admin_get_settings():
    settings = SiteSettings.query.all()
    api_keys = APIKey.query.all()
    
    return jsonify({
        'settings': {s.key: s.value for s in settings},
        'api_keys': [{
            'id': k.id,
            'service_name': k.service_name,
            'api_key': k.api_key[:8] + '...' + k.api_key[-4:] if len(k.api_key) > 12 else '***',
            'is_active': k.is_active,
            'usage_count': k.usage_count,
            'last_used': k.last_used.isoformat() if k.last_used else None
        } for k in api_keys],
        'available_models': LLMEngine.MODELS
    })


@app.route('/api/admin/settings', methods=['PUT'])
@login_required
@admin_required
def admin_update_settings():
    data = request.json
    for key, value in data.items():
        set_setting(key, value)
    sys_log(f"Settings updated by {current_user.username}", 'info', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/api-keys', methods=['POST'])
@login_required
@admin_required
def admin_add_api_key():
    data = request.json
    service = bleach.clean(data.get('service_name', '').strip())
    key = data.get('api_key', '').strip()
    
    if not service or not key:
        return jsonify({'error': 'Service name and API key required'}), 400
    
    existing = APIKey.query.filter_by(service_name=service).first()
    if existing:
        existing.api_key = key
        existing.is_active = True
    else:
        db.session.add(APIKey(service_name=service, api_key=key))
    
    db.session.commit()
    sys_log(f"API key updated for {service} by {current_user.username}", 'info', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/api-keys/<int:key_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_api_key(key_id):
    key = APIKey.query.get(key_id)
    if key:
        db.session.delete(key)
        db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/api-keys/<int:key_id>/toggle', methods=['PUT'])
@login_required
@admin_required
def admin_toggle_api_key(key_id):
    key = APIKey.query.get(key_id)
    if key:
        key.is_active = not key.is_active
        db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/users', methods=['GET'])
@login_required
@admin_required
def admin_get_users():
    users = User.query.order_by(desc(User.created_at)).all()
    return jsonify([{
        'id': u.id,
        'username': u.username,
        'email': u.email or '',
        'is_admin': u.is_admin,
        'is_active': u.is_active_user,
        'is_banned': u.is_banned,
        'total_messages': u.total_messages,
        'created_at': u.created_at.isoformat(),
        'last_login': u.last_login.isoformat() if u.last_login else ''
    } for u in users])


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_user(user_id):
    if user_id == current_user.id:
        return jsonify({'error': 'Cannot delete yourself'}), 400
    user = User.query.get(user_id)
    if user:
        db.session.delete(user)
        db.session.commit()
        sys_log(f"User {user.username} deleted by {current_user.username}", 'warning', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/users/<int:user_id>/ban', methods=['PUT'])
@login_required
@admin_required
def admin_ban_user(user_id):
    if user_id == current_user.id:
        return jsonify({'error': 'Cannot ban yourself'}), 400
    user = User.query.get(user_id)
    if user:
        user.is_banned = not user.is_banned
        db.session.commit()
        action = 'banned' if user.is_banned else 'unbanned'
        sys_log(f"User {user.username} {action} by {current_user.username}", 'warning', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/users/<int:user_id>/toggle-admin', methods=['PUT'])
@login_required
@admin_required
def admin_toggle_admin(user_id):
    if user_id == current_user.id:
        return jsonify({'error': 'Cannot change own admin status'}), 400
    user = User.query.get(user_id)
    if user:
        user.is_admin = not user.is_admin
        db.session.commit()
        sys_log(f"User {user.username} admin={'granted' if user.is_admin else 'revoked'} by {current_user.username}", 'warning', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/users/<int:user_id>/reset-password', methods=['PUT'])
@login_required
@admin_required
def admin_reset_password(user_id):
    data = request.json
    new_password = data.get('password', '').strip()
    if not new_password or len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 chars'}), 400
    
    user = User.query.get(user_id)
    if user:
        user.set_password(new_password)
        db.session.commit()
        sys_log(f"Password reset for {user.username} by {current_user.username}", 'warning', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/change-password', methods=['PUT'])
@login_required
@admin_required
def admin_change_own_password():
    data = request.json
    current_pw = data.get('current_password', '')
    new_pw = data.get('new_password', '')
    
    if not current_user.check_password(current_pw):
        return jsonify({'error': 'Current password is incorrect'}), 400
    if len(new_pw) < 6:
        return jsonify({'error': 'New password must be at least 6 chars'}), 400
    
    current_user.set_password(new_pw)
    db.session.commit()
    sys_log(f"Admin {current_user.username} changed own password", 'info', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/announcements', methods=['GET'])
@login_required
@admin_required
def admin_get_announcements():
    anns = Announcement.query.order_by(desc(Announcement.created_at)).all()
    return jsonify([{
        'id': a.id,
        'title': a.title,
        'content': a.content,
        'is_active': a.is_active,
        'priority': a.priority,
        'created_at': a.created_at.isoformat()
    } for a in anns])


@app.route('/api/admin/announcements', methods=['POST'])
@login_required
@admin_required
def admin_create_announcement():
    data = request.json
    ann = Announcement(
        title=bleach.clean(data.get('title', '')),
        content=bleach.clean(data.get('content', '')),
        priority=data.get('priority', 'normal'),
        is_active=True
    )
    db.session.add(ann)
    db.session.commit()
    return jsonify({'success': True, 'id': ann.id})


@app.route('/api/admin/announcements/<int:ann_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_announcement(ann_id):
    ann = Announcement.query.get(ann_id)
    if ann:
        db.session.delete(ann)
        db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/announcements/<int:ann_id>/toggle', methods=['PUT'])
@login_required
@admin_required
def admin_toggle_announcement(ann_id):
    ann = Announcement.query.get(ann_id)
    if ann:
        ann.is_active = not ann.is_active
        db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/commands', methods=['GET'])
@login_required
@admin_required
def admin_get_commands():
    cmds = CustomCommand.query.all()
    return jsonify([{
        'id': c.id,
        'command': c.command,
        'response': c.response,
        'is_active': c.is_active
    } for c in cmds])


@app.route('/api/admin/commands', methods=['POST'])
@login_required
@admin_required
def admin_create_command():
    data = request.json
    cmd = CustomCommand(
        command=bleach.clean(data.get('command', '')),
        response=data.get('response', ''),
        is_active=True
    )
    db.session.add(cmd)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/commands/<int:cmd_id>', methods=['DELETE'])
@login_required
@admin_required
def admin_delete_command(cmd_id):
    cmd = CustomCommand.query.get(cmd_id)
    if cmd:
        db.session.delete(cmd)
        db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/shutdown', methods=['POST'])
@login_required
@admin_required
def admin_shutdown():
    data = request.json
    action = data.get('action', 'toggle')
    current = get_setting('shutdown_mode', 'false')
    
    if action == 'toggle':
        new_val = 'false' if current == 'true' else 'true'
    elif action == 'on':
        new_val = 'true'
    else:
        new_val = 'false'
    
    set_setting('shutdown_mode', new_val)
    sys_log(f"Shutdown mode {'activated' if new_val == 'true' else 'deactivated'} by {current_user.username}", 'critical', 'admin')
    return jsonify({'success': True, 'shutdown_mode': new_val})


@app.route('/api/admin/maintenance', methods=['POST'])
@login_required
@admin_required
def admin_maintenance():
    data = request.json
    current = get_setting('maintenance_mode', 'false')
    new_val = 'false' if current == 'true' else 'true'
    set_setting('maintenance_mode', new_val)
    
    if 'message' in data:
        set_setting('maintenance_message', data['message'])
    
    sys_log(f"Maintenance mode {'activated' if new_val == 'true' else 'deactivated'} by {current_user.username}", 'warning', 'admin')
    return jsonify({'success': True, 'maintenance_mode': new_val})


@app.route('/api/admin/clear-chats', methods=['POST'])
@login_required
@admin_required
def admin_clear_all_chats():
    Message.query.delete()
    Chat.query.delete()
    db.session.commit()
    sys_log(f"All chats cleared by {current_user.username}", 'warning', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/clear-analytics', methods=['POST'])
@login_required
@admin_required
def admin_clear_analytics():
    AnalyticsEvent.query.delete()
    db.session.commit()
    sys_log(f"Analytics cleared by {current_user.username}", 'info', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/clear-logs', methods=['POST'])
@login_required
@admin_required
def admin_clear_logs():
    SystemLog.query.delete()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/admin/export-data', methods=['GET'])
@login_required
@admin_required
def admin_export_data():
    data = {
        'users': [{
            'username': u.username,
            'email': u.email,
            'is_admin': u.is_admin,
            'total_messages': u.total_messages,
            'created_at': u.created_at.isoformat()
        } for u in User.query.all()],
        'settings': {s.key: s.value for s in SiteSettings.query.all()},
        'total_chats': Chat.query.count(),
        'total_messages': Message.query.count(),
        'exported_at': datetime.utcnow().isoformat()
    }
    return jsonify(data)


@app.route('/api/admin/broadcast', methods=['POST'])
@login_required
@admin_required
def admin_broadcast():
    data = request.json
    message = data.get('message', '')
    if message:
        socketio.emit('broadcast', {'message': message, 'from': 'admin'}, broadcast=True)
        sys_log(f"Broadcast sent by {current_user.username}: {message[:100]}", 'info', 'admin')
    return jsonify({'success': True})


@app.route('/api/admin/sources', methods=['GET'])
@login_required
@admin_required
def admin_get_sources():
    sources = []
    for name, info in KnowledgeEngine.SOURCES.items():
        sources.append({
            'name': name,
            'url': info['url'],
            'category': info['category']
        })
    return jsonify({
        'total': len(sources),
        'sources': sources,
        'categories': list(set(s['category'] for s in sources))
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# USER SETTINGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route('/api/user/settings', methods=['GET'])
@login_required
def user_settings():
    return jsonify({
        'username': current_user.username,
        'email': current_user.email or '',
        'theme': current_user.theme,
        'total_messages': current_user.total_messages,
        'created_at': current_user.created_at.isoformat()
    })


@app.route('/api/user/change-password', methods=['PUT'])
@login_required
def user_change_password():
    data = request.json
    if not current_user.check_password(data.get('current_password', '')):
        return jsonify({'error': 'Current password incorrect'}), 400
    if len(data.get('new_password', '')) < 6:
        return jsonify({'error': 'New password must be 6+ chars'}), 400
    current_user.set_password(data['new_password'])
    db.session.commit()
    return jsonify({'success': True})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SOCKETIO EVENTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        emit('connected', {'user': current_user.username})


@socketio.on('typing')
def handle_typing(data):
    pass  # Can be used for typing indicators


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ERROR HANDLERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.errorhandler(403)
def forbidden(e):
    return jsonify({'error': 'Access denied'}), 403

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({'error': 'Internal server error'}), 500


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RUN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
    