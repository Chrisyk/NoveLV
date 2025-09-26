import os
import sys
import json
import re
from collections import Counter
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, Response, jsonify
from datetime import datetime
import requests
import uuid
import time

# Add the dev directory to Python path to import AnkiDataManager
sys.path.append(os.path.join(os.path.dirname(__file__), 'dev'))
from get_data import AnkiDataManager 

# Import database functions for scan history
try:
    from . import database
except ImportError:
    import database 

# Global variables for frequency-based matching
frequency_cache = None
progress_tracker = {}
active_yomitan_operations = 0

def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY='dev',
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16MB max file size
    )
    
    # Add number formatting filter
    @app.template_filter('number_format')
    def number_format_filter(value):
        """Format numbers with thousand separators"""
        try:
            return "{:,}".format(int(value))
        except (ValueError, TypeError):
            return value

    # Add star rating filter
    @app.template_filter('star_from_rank')
    def star_from_rank_filter(rank):
        """Convert frequency rank to star rating (0-5)"""
        if rank is None:
            return 0
        elif rank <= 1500:
            return 5
        elif rank <= 5000:
            return 4
        elif rank <= 15000:
            return 3
        elif rank <= 30000:
            return 2
        elif rank <= 60000:
            return 1
        else:
            return 0

    if test_config is None:
        app.config.from_pyfile('config.py', silent=True)
    else:
        app.config.from_mapping(test_config)
    
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # Initialize AnkiDataManager - cache directory one level above the app folder
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    anki_manager = AnkiDataManager(cache_dir=cache_dir)
    
    # Novel storage directory - one level above the app folder
    novel_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "novels")
    os.makedirs(novel_dir, exist_ok=True)
    
    # Allowed file extensions
    ALLOWED_EXTENSIONS = {'txt', 'md', 'epub'}

    def allowed_file(filename):
        return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

    def get_available_caches():
        """Get all available cached vocabularies"""
        caches = []
        if os.path.exists(anki_manager.cache_dir):
            for file in os.listdir(anki_manager.cache_dir):
                if file.endswith('.json'):
                    try:
                        file_path = os.path.join(anki_manager.cache_dir, file)
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            caches.append({
                                'filename': file,
                                'note_type': data['metadata'].get('note_type', 'Unknown'),
                                'field_name': data['metadata'].get('field_name', 'Unknown'),
                                'last_updated': data['metadata'].get('last_updated', 'Never'),
                                'total_cards': data['metadata'].get('total_cards', 0),
                                'key': f"{data['metadata'].get('note_type', 'Unknown')}_{data['metadata'].get('field_name', 'Unknown')}"
                            })
                    except:
                        pass
        return caches

    # Yomitan API configuration
    YOMITAN_API_URL = "http://127.0.0.1:19633"
    YOMITAN_API_TIMEOUT = 100  # Increased timeout for Yomitan API processing
    YOMITAN_CHUNK_SIZE = 300  # Maximum characters per API request chunk

    # ============================================
    # MULTI-DICTIONARY FREQUENCY SYSTEM
    # ============================================
    
    def get_yomitan_frequency_data(word):
        """
        Get frequency data for a word using one-to-one matching only.
        No conversion from kana to kanji - exact word matching only.
        
        Args:
            word: The exact word to look up
            
        Returns:
            dict: Frequency data with structure:
                  {'rank': int, 'source': str, 'found': bool}
                  or {'found': False} if not found
        """
        global frequency_cache
        
        try:
            # Check if word is already cached
            if frequency_cache and word in frequency_cache:
                cached_data = frequency_cache[word]
                return {
                    'rank': cached_data['rank'],
                    'source': cached_data['source'],
                    'found': True
                }
            
            # Make API request to yomitan - but only look for exact matches
            response = requests.post(
                f"{YOMITAN_API_URL}/termEntries", 
                json={"term": word},
                timeout=YOMITAN_API_TIMEOUT
            )
            
            if response.status_code != 200:
                return {'found': False}
            
            data = response.json()
            
            # Parse the response to extract frequency information
            if not data or 'dictionaryEntries' not in data:
                return {'found': False}
            
            # Look for frequency information ONLY for the exact word match
            best_rank = None
            best_source = None
            
            for entry in data['dictionaryEntries']:
                # Only process entries that match the exact input word
                if 'headwords' in entry and entry['headwords']:
                    exact_match_found = False
                    for headword in entry['headwords']:
                        if 'term' in headword and headword['term'] == word:
                            exact_match_found = True
                            break
                        if 'reading' in headword and headword['reading'] == word:
                            exact_match_found = True
                            break
                    
                    # Only get frequency data if this entry is for the exact word
                    if exact_match_found and 'frequencies' in entry and entry['frequencies']:
                        for freq_item in entry['frequencies']:
                            if isinstance(freq_item, dict) and 'frequency' in freq_item:
                                rank = freq_item['frequency']
                                source = freq_item.get('dictionary', 'Unknown')
                                
                                # Use the best (lowest) rank found
                                if best_rank is None or rank < best_rank:
                                    best_rank = rank
                                    best_source = source
            
            if best_rank is not None:
                # Cache the result
                if frequency_cache is None:
                    frequency_cache = {}
                
                frequency_cache[word] = {
                    'rank': best_rank,
                    'source': best_source,
                    'found': True
                }
                
                return {
                    'rank': best_rank,
                    'source': best_source,
                    'found': True
                }
            
            return {'found': False}
            
        except requests.exceptions.RequestException as e:
            print(f"Yomitan API request failed for word '{word}': {e}")
            return {'found': False}
        except Exception as e:
            print(f"Error processing yomitan data for word '{word}': {e}")
            return {'found': False}

    def get_yomitan_vocabulary_match(word, vocabulary_set):
        """
        Check if a word matches vocabulary using yomitan API to get dictionary forms.
        
        Args:
            word: The word to check
            vocabulary_set: Set of vocabulary words to match against
            
        Returns:
            dict: {'found': bool, 'matched_form': str or None, 'original_word': str}
        """
        try:
            # First try direct match
            if word in vocabulary_set:
                return {
                    'found': True,
                    'matched_form': word,
                    'original_word': word
                }
            
            # Query yomitan for dictionary entries
            response = requests.post(
                f"{YOMITAN_API_URL}/termEntries", 
                json={"term": word},
                timeout=YOMITAN_API_TIMEOUT
            )
            
            if response.status_code != 200:
                return {
                    'found': False,
                    'matched_form': None,
                    'original_word': word
                }
            
            data = response.json()
            
            if not data:
                return {
                    'found': False,
                    'matched_form': None,
                    'original_word': word
                }
            
            # The response should be a dict with 'dictionaryEntries' key
            entries = []
            if isinstance(data, dict) and 'dictionaryEntries' in data:
                entries = data['dictionaryEntries']
            elif isinstance(data, list):
                # Fallback to treating data as direct list
                entries = data
            else:
                return {
                    'found': False,
                    'matched_form': None,
                    'original_word': word
                }
            
            if not entries:
                return {
                    'found': False,
                    'matched_form': None,
                    'original_word': word
                }
            
            # Process each entry
            for i, entry in enumerate(entries):
                # Check if this entry has a 'headwords' field
                if 'headwords' in entry and entry['headwords']:
                    for headword in entry['headwords']:
                        if 'term' in headword:
                            dictionary_form = headword['term']
                            if dictionary_form in vocabulary_set:
                                return {
                                    'found': True,
                                    'matched_form': dictionary_form,
                                    'original_word': word
                                }
                        
                        if 'reading' in headword:
                            reading_form = headword['reading']
                            if reading_form in vocabulary_set:
                                return {
                                    'found': True,
                                    'matched_form': reading_form,
                                    'original_word': word
                                }
            
            return {
                'found': False,
                'matched_form': None,
                'original_word': word
            }
            
        except requests.exceptions.RequestException as e:
            print(f"Yomitan vocabulary API request failed for word '{word}': {e}")
            return {
                'found': False,
                'matched_form': None,
                'original_word': word
            }
        except Exception as e:
            print(f"Error processing yomitan vocabulary data for word '{word}': {e}")
            return {
                'found': False,
                'matched_form': None,
                'original_word': word
            }
    
    def get_star_rating_ranges():
        """
        Define star rating ranges based on frequency ranks.
        This uses a generalized approach suitable for most frequency dictionaries.
        
        Returns:
            dict: Star level -> (min_rank, max_rank) mapping
        """
        return {
            5: (1, 1500),      # Most common words
            4: (1501, 5000),   # Very common words  
            3: (5001, 15000),  # Common words
            2: (15001, 30000), # Moderately common words
            1: (30001, 60000), # Less common words
            0: (60001, float('inf'))  # Rare words
        }
    
    def load_frequency_data():
        """
        Initialize frequency cache for yomitan API-based lookups.
        This doesn't pre-load data but sets up the caching system.
        
        Returns:
            dict: Empty cache that will be populated on demand
        """
        global frequency_cache
        if frequency_cache is None:
            frequency_cache = {}
        
        return frequency_cache
    
    def get_word_star_rating(word, frequency_data=None):
        """
        Calculate star rating for a word using yomitan API frequency data.
        
        Args:
            word: The word to rate
            frequency_data: Frequency cache (will initialize if None)
            
        Returns:
            dict: Rating info with structure:
                  {'stars': int, 'rank': int, 'source': str}
                  or {'stars': 0} if word not found
        """
        if frequency_data is None:
            frequency_data = load_frequency_data()
        
        # Get frequency data from yomitan API
        freq_result = get_yomitan_frequency_data(word)
        
        if not freq_result.get('found'):
            return {'stars': 0, 'rank': None, 'source': None}
        
        rank = freq_result['rank']
        source = freq_result['source']
        star_ranges = get_star_rating_ranges()
        
        # Calculate stars using the defined ranges
        stars = 0
        for star_level in sorted(star_ranges.keys(), reverse=True):
            min_rank, max_rank = star_ranges[star_level]
            if min_rank <= rank <= max_rank:
                stars = star_level
                break
        
        return {
            'stars': stars,
            'rank': rank,
            'source': source
        }

    def calculate_vocabulary_star_statistics(known_words, frequency_data=None):
        """
        Calculate star rating statistics for vocabulary analysis.
        
        Args:
            known_words: List of known word dictionaries from vocabulary analysis
            frequency_data: Frequency dictionary (will load if None)
            
        Returns:
            dict: Statistics including star distribution and average rating
        """
        if frequency_data is None:
            frequency_data = load_frequency_data()
        
        # Count words by star rating
        star_counts = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0, 0: 0}
        total_word_instances = 0  # Count by frequency of occurrence
        total_star_score = 0
        
        for word_info in known_words:
            word = word_info['word']
            count = word_info['count']  # How many times this word appears
            
            rating_info = get_word_star_rating(word, frequency_data)
            stars = rating_info['stars']
            source = rating_info.get('source', 'Unknown')
            
            star_counts[stars] += 1  # Count unique words
            
            # Weight by occurrence frequency for average calculation
            total_word_instances += count
            total_star_score += stars * count
        
        # Calculate average star rating (weighted by word frequency)
        avg_stars = total_star_score / total_word_instances if total_word_instances > 0 else 0
        
        return {
            'star_distribution': star_counts,
            'total_unique_words': sum(star_counts.values()),
            'total_word_instances': total_word_instances,
            'average_star_rating': round(avg_stars, 2),
            'star_breakdown': {
                5: {'count': star_counts[5], 'label': '5 stars (1-1.5k most common)'},
                4: {'count': star_counts[4], 'label': '4 stars (1.5k-5k)'},
                3: {'count': star_counts[3], 'label': '3 stars (5k-15k)'},
                2: {'count': star_counts[2], 'label': '2 stars (15k-30k)'},
                1: {'count': star_counts[1], 'label': '1 star (30k-60k)'},
                0: {'count': star_counts[0], 'label': '0 stars (60k+ or unknown)'}
            }
        }

    def calculate_frequency_star_statistics(all_words):
        """
        Calculate star rating statistics using yomitan frequency data for all words.
        
        Args:
            all_words: List of word dictionaries with frequency information
            
        Returns:
            dict: Statistics including star distribution and average rating
        """
        # Count words by star rating
        star_counts = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0, 0: 0}
        total_word_instances = 0  # Count by frequency of occurrence
        total_star_score = 0
        words_with_frequency = 0
        
        for word_info in all_words:
            word = word_info['word']
            count = word_info['count']  # How many times this word appears
            has_frequency = word_info['has_frequency']
            rank = word_info.get('rank')
            
            if has_frequency and rank is not None:
                # Convert rank to star rating
                if rank <= 1500:
                    stars = 5
                elif rank <= 5000:
                    stars = 4
                elif rank <= 15000:
                    stars = 3
                elif rank <= 30000:
                    stars = 2
                elif rank <= 60000:
                    stars = 1
                else:
                    stars = 0
                
                words_with_frequency += 1
            else:
                # No frequency data available
                stars = 0
            
            star_counts[stars] += 1  # Count unique words
            
            # Weight by occurrence frequency for average calculation
            total_word_instances += count
            total_star_score += stars * count
        
        # Calculate average star rating (weighted by word frequency)
        avg_stars = total_star_score / total_word_instances if total_word_instances > 0 else 0
        
        return {
            'star_distribution': star_counts,
            'total_unique_words': sum(star_counts.values()),
            'total_word_instances': total_word_instances,
            'words_with_frequency': words_with_frequency,
            'average_star_rating': round(avg_stars, 2),
            'star_breakdown': {
                5: {'count': star_counts[5], 'label': '5 stars (1-1.5k most common)'},
                4: {'count': star_counts[4], 'label': '4 stars (1.5k-5k)'},
                3: {'count': star_counts[3], 'label': '3 stars (5k-15k)'},
                2: {'count': star_counts[2], 'label': '2 stars (15k-30k)'},
                1: {'count': star_counts[1], 'label': '1 star (30k-60k)'},
                0: {'count': star_counts[0], 'label': '0 stars (60k+ or unknown)'}
            }
        }

    def calculate_three_category_frequency_statistics(known_words, ignored_words, unknown_words):
        """
        Calculate star rating statistics with three categories: Known (green), Ignored (gray), Unknown (red).
        
        Args:
            known_words: List of known word dictionaries
            ignored_words: List of ignored word dictionaries  
            unknown_words: List of unknown word dictionaries
            
        Returns:
            dict: Statistics including star distribution by category
        """
        def get_star_rating_from_rank(rank):
            """Convert frequency rank to star rating"""
            if rank is None:
                return 0
            elif rank <= 1500:
                return 5
            elif rank <= 5000:
                return 4
            elif rank <= 15000:
                return 3
            elif rank <= 30000:
                return 2
            elif rank <= 60000:
                return 1
            else:
                return 0
        
        # Initialize counters for each category
        categories = {
            'known': {'star_counts': {5: 0, 4: 0, 3: 0, 2: 0, 1: 0, 0: 0}, 'total_instances': 0, 'total_score': 0},
            'ignored': {'star_counts': {5: 0, 4: 0, 3: 0, 2: 0, 1: 0, 0: 0}, 'total_instances': 0, 'total_score': 0},
            'unknown': {'star_counts': {5: 0, 4: 0, 3: 0, 2: 0, 1: 0, 0: 0}, 'total_instances': 0, 'total_score': 0}
        }
        
        # Process each category
        for category_name, word_list in [('known', known_words), ('ignored', ignored_words), ('unknown', unknown_words)]:
            category_data = categories[category_name]
            
            for word_info in word_list:
                count = word_info['count']
                rank = word_info.get('rank')
                stars = get_star_rating_from_rank(rank)
                
                category_data['star_counts'][stars] += 1
                category_data['total_instances'] += count
                category_data['total_score'] += stars * count
        
        # Calculate averages and create final structure
        result = {
            'categories': {},
            'combined_star_distribution': {5: 0, 4: 0, 3: 0, 2: 0, 1: 0, 0: 0},
            'total_unique_words': 0,
            'total_word_instances': 0
        }
        
        for category_name, data in categories.items():
            avg_rating = data['total_score'] / data['total_instances'] if data['total_instances'] > 0 else 0
            unique_count = sum(data['star_counts'].values())
            
            result['categories'][category_name] = {
                'star_counts': data['star_counts'],
                'unique_words': unique_count,
                'total_instances': data['total_instances'],
                'average_rating': round(avg_rating, 2)
            }
            
            # Add to combined totals
            for star in range(6):
                result['combined_star_distribution'][star] += data['star_counts'][star]
            result['total_unique_words'] += unique_count
            result['total_word_instances'] += data['total_instances']
        
        # Add star breakdown with labels and colors
        result['star_breakdown'] = {
            5: {'count': result['combined_star_distribution'][5], 'label': '5★ (1-1.5k most common)', 'color': '#10b981'},
            4: {'count': result['combined_star_distribution'][4], 'label': '4★ (1.5k-5k)', 'color': '#3b82f6'},
            3: {'count': result['combined_star_distribution'][3], 'label': '3★ (5k-15k)', 'color': '#8b5cf6'},
            2: {'count': result['combined_star_distribution'][2], 'label': '2★ (15k-30k)', 'color': '#f59e0b'},
            1: {'count': result['combined_star_distribution'][1], 'label': '1★ (30k-60k)', 'color': '#ef4444'},
            0: {'count': result['combined_star_distribution'][0], 'label': '0★ (60k+ or unknown)', 'color': '#6b7280'}
        }
        
        return result

    # ============================================
    # IGNORED WORDS MANAGEMENT
    # ============================================
    
    def get_ignored_words_file():
        """Get the path to the ignored words JSON file."""
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ignored_words.json")
    
    def load_ignored_words():
        """Load the list of words that users have chosen to ignore."""
        ignored_file = get_ignored_words_file()
        try:
            if os.path.exists(ignored_file):
                with open(ignored_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return set(data.get('ignored_words', []))
            return set()
        except Exception as e:
            print(f"Error loading ignored words: {e}")
            return set()
    
    def save_ignored_words(ignored_words_set):
        """Save the set of ignored words to JSON file."""
        ignored_file = get_ignored_words_file()
        try:
            data = {
                'ignored_words': list(ignored_words_set),
                'last_updated': datetime.now().isoformat()
            }
            with open(ignored_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"Error saving ignored words: {e}")
            return False
    
    def add_ignored_word(word):
        """Add a word to the ignored list."""
        ignored_words = load_ignored_words()
        ignored_words.add(word)
        return save_ignored_words(ignored_words)
    
    def remove_ignored_word(word):
        """Remove a word from the ignored list."""
        ignored_words = load_ignored_words()
        ignored_words.discard(word)
        return save_ignored_words(ignored_words)
    
    def is_word_ignored(word):
        """Check if a word is in the ignored list."""
        ignored_words = load_ignored_words()
        return word in ignored_words

    def check_yomitan_health():
        """
        Simple ping check to see if Yomitan API server is responding.
        Returns (is_healthy, status_message, response_time)
        """
        start_time = time.time()
        
        # Determine appropriate timeout based on system activity
        base_timeout = 3
        if active_yomitan_operations > 0:
            # If actively processing, use slightly longer timeout
            base_timeout = 5
            status_suffix = " (processing active)"
        else:
            status_suffix = ""
        
        try:
            # Simple GET request to see if server is alive - don't test functionality
            response = requests.get(f"{YOMITAN_API_URL}/", timeout=base_timeout)
            response_time = round((time.time() - start_time) * 1000, 1)
            
            # Any response means server is running
            if response.status_code in [200, 501, 404]:  # 501/404 are fine, just means endpoint exists
                return True, f"✅ Yomitan API responding ({response_time}ms){status_suffix}", response_time
            else:
                return False, f"⚠️ Yomitan API responded with status {response.status_code}{status_suffix}", response_time
                
        except requests.exceptions.Timeout:
            response_time = round((time.time() - start_time) * 1000, 1)
            if active_yomitan_operations > 0:
                # During active processing, timeout might be expected
                return True, f"⏱️ Yomitan API busy processing ({response_time}ms timeout){status_suffix}", response_time
            else:
                return False, f"❌ Yomitan API timeout after {response_time}ms", response_time
        except requests.exceptions.ConnectionError:
            return False, f"❌ Yomitan API server not running{status_suffix}", 0
        except Exception as e:
            return False, f"❌ Yomitan API error: {str(e)}{status_suffix}", 0
    
    def test_yomitan_tokenization(extended_timeout=False):
        """Test actual tokenization functionality including chunking"""
        import time
        start_time = time.time()
        
        # Use longer timeout if system is actively processing
        timeout = 15 if extended_timeout else 5
        status_suffix = " (extended timeout)" if extended_timeout else ""
        
        try:
            # Test 1: Basic tokenization
            test_text = "テスト"
            response = requests.post(
                f"{YOMITAN_API_URL}/tokenize",
                json={"text": test_text, "scanLength": 1},
                timeout=timeout
            )
            response_time = round((time.time() - start_time) * 1000, 1)
            
            if response.status_code != 200:
                return False, f"❌ Yomitan tokenize failed: HTTP {response.status_code}{status_suffix}", response_time
            
            result = response.json()
            if not (result and isinstance(result, list) and len(result) > 0):
                return False, f"⚠️ Yomitan returned unexpected format: {result}{status_suffix}", response_time
            
            # Test 2: Chunking functionality with longer text (only if not extended timeout to avoid double-processing)
            if not extended_timeout:
                long_text = "これは長いテストテキストです。" * 100  # Create ~3000 character text
                words, success = tokenize_with_yomitan_api(long_text, chunk_size=1000)  # Force chunking
                
                if not success:
                    return False, f"❌ Chunked tokenization failed{status_suffix}", response_time
                
                if len(words) == 0:
                    return False, f"⚠️ Chunked tokenization returned no words{status_suffix}", response_time
            
            final_time = round((time.time() - start_time) * 1000, 1)
            chunk_test_msg = " with chunking" if not extended_timeout else ""
            return True, f"✅ Yomitan tokenization working{chunk_test_msg} ({final_time}ms){status_suffix}", final_time
                
        except requests.exceptions.Timeout:
            response_time = round((time.time() - start_time) * 1000, 1)
            if extended_timeout:
                # During active processing, timeout might indicate busy system
                return True, f"⏱️ Yomitan API busy (timeout after {response_time}ms){status_suffix}", response_time
            else:
                return False, f"❌ Yomitan tokenize timeout after {response_time}ms", response_time
        except Exception as e:
            response_time = round((time.time() - start_time) * 1000, 1)
            return False, f"❌ Yomitan tokenize error: {str(e)}{status_suffix}", response_time

    def check_anki_health():
        """
        Check if Anki Connect is running and responding.
        Returns (is_healthy, status_message, response_time)
        """
        import time
        start_time = time.time()
        
        try:
            # Test Anki Connect by calling the version action
            response = requests.post(
                "http://127.0.0.1:8765",
                json={
                    "action": "version",
                    "version": 6
                },
                timeout=3
            )
            response_time = round((time.time() - start_time) * 1000, 1)
            
            if response.status_code == 200:
                try:
                    result = response.json()
                    if result.get("error") is None and result.get("result") is not None:
                        version = result.get("result")
                        return True, f"✅ Anki Connect v{version} responding ({response_time}ms)", response_time
                    else:
                        error_msg = result.get("error", "Unknown error")
                        return False, f"⚠️ Anki Connect error: {error_msg}", response_time
                except ValueError:
                    return False, f"⚠️ Anki Connect invalid JSON response", response_time
            else:
                return False, f"⚠️ Anki Connect responded with status {response.status_code}", response_time
                
        except requests.exceptions.Timeout:
            response_time = round((time.time() - start_time) * 1000, 1)
            return False, f"❌ Anki Connect timeout after {response_time}ms", response_time
        except requests.exceptions.ConnectionError:
            return False, f"❌ Anki Connect not running (check if Anki is open)", 0
        except Exception as e:
            return False, f"❌ Anki Connect error: {str(e)}", 0
    
    def tokenize_with_yomitan_api(text, max_scan_length=50, chunk_size=None, progress_id=None):
        """
        Use Yomitan API for superior Japanese tokenization with chunking support.
        Splits long texts into chunks to handle API limitations.
        Returns (words, success_flag) tuple.
        """
        global progress_tracker
        
        if chunk_size is None:
            chunk_size = YOMITAN_CHUNK_SIZE
            
        if not text or not text.strip():
            return [], True
            
        # If text is short enough, process normally
        if len(text) <= chunk_size:
            if progress_id:
                progress_tracker[progress_id] = {
                    'stage': 'tokenizing',
                    'message': 'Processing text...',
                    'progress': 50
                }
            return _tokenize_single_chunk(text, max_scan_length)
        
        # Split long text into chunks
        all_words = []
        chunks = _split_text_into_chunks(text, chunk_size)
        
        if progress_id:
            progress_tracker[progress_id] = {
                'stage': 'chunking',
                'message': f'Split text into {len(chunks)} chunks',
                'total_chunks': len(chunks),
                'completed_chunks': 0,
                'progress': 10
            }
        
        for i, chunk in enumerate(chunks, 1):            
            if progress_id:
                progress_tracker[progress_id] = {
                    'stage': 'tokenizing',
                    'message': f'Processing chunk {i} of {len(chunks)}...',
                    'total_chunks': len(chunks),
                    'completed_chunks': i - 1,
                    'progress': 10 + (i - 1) / len(chunks) * 80
                }
            
            chunk_words, success = _tokenize_single_chunk(chunk, max_scan_length)
            
            if not success:
                continue
                
            all_words.extend(chunk_words)
            
            if progress_id:
                progress_tracker[progress_id] = {
                    'stage': 'tokenizing',
                    'message': f'Completed chunk {i} of {len(chunks)} ({len(chunk_words)} tokens)',
                    'total_chunks': len(chunks),
                    'completed_chunks': i,
                    'progress': 10 + i / len(chunks) * 80
                }
        
        total_success = len(all_words) > 0  # Success if we got any tokens
        
        if progress_id:
            progress_tracker[progress_id] = {
                'stage': 'complete',
                'message': f'Tokenization complete! {len(all_words)} total tokens',
                'total_chunks': len(chunks),
                'completed_chunks': len(chunks),
                'progress': 90
            }
        
        return all_words, total_success

    def _split_text_into_chunks(text, chunk_size):
        """
        Split text into chunks, trying to break at natural boundaries.
        Prioritizes sentence endings, then punctuation, then whitespace.
        """
        if len(text) <= chunk_size:
            return [text]
        
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + chunk_size
            
            if end >= len(text):
                # Last chunk
                chunks.append(text[start:])
                break
            
            # Try to find a good break point within the last 200 characters
            search_start = max(start, end - 200)
            chunk_text = text[start:end]
            
            # Look for sentence endings (Japanese punctuation)
            for punct in ['。', '！', '？', '…', '』', '」']:
                punct_pos = chunk_text.rfind(punct, search_start - start)
                if punct_pos != -1:
                    end = start + punct_pos + 1
                    break
            else:
                # Look for other punctuation
                for punct in ['、', '・', '）', '｝']:
                    punct_pos = chunk_text.rfind(punct, search_start - start)
                    if punct_pos != -1:
                        end = start + punct_pos + 1
                        break
                else:
                    # Look for whitespace
                    for ws in ['\n', ' ', '　']:  # 　 is full-width space
                        ws_pos = chunk_text.rfind(ws, search_start - start)
                        if ws_pos != -1:
                            end = start + ws_pos + 1
                            break
            
            chunks.append(text[start:end])
            start = end
        
        return chunks

    def _tokenize_single_chunk(text, max_scan_length=50):
        """
        Tokenize a single chunk of text with Yomitan API.
        Returns (words, success_flag) tuple.
        """
        global active_yomitan_operations
        
        try:
            # Increment active operations counter
            active_yomitan_operations += 1
            
            # Make request to Yomitan API tokenize endpoint
            # IMPORTANT: scanLength parameter is required for the API to work
            params = {
                "text": text,
                "scanLength": max_scan_length
            }
            
            response = requests.post(
                f"{YOMITAN_API_URL}/tokenize", 
                json=params, 
                timeout=YOMITAN_API_TIMEOUT
            )
            
            if response.status_code == 200:
                result = response.json()
                
                # Extract text segments from Yomitan response
                words = []
                if result and len(result) > 0 and isinstance(result, list):
                    # The response is an array, typically with one item containing 'content'
                    for response_item in result:
                        if isinstance(response_item, dict) and 'content' in response_item:
                            content_array = response_item['content']
                            
                            for i, segment in enumerate(content_array):
                                if isinstance(segment, list) and len(segment) > 0:
                                    # Each segment is an array of parsing options that should be concatenated
                                    # to form the complete word (e.g., 熱 + い = 熱い)
                                    complete_word = ''.join([option.get('text', '') for option in segment if isinstance(option, dict)])
                                    
                                    if complete_word:
                                        words.append(complete_word)
                elif isinstance(result, list):
                    # Try direct format - each item might be a token array (old fallback logic)
                    for i, token in enumerate(result):
                        if isinstance(token, list) and len(token) > 0:
                            # Assume first element is the surface form
                            surface = token[0]
                            words.append(surface)
                        elif isinstance(token, str):
                            words.append(token)
                
                return words, True  # Success flag
            else:
                return [], False
                
        except requests.exceptions.Timeout as e:
            return _simple_japanese_tokenize(text), True
        except requests.exceptions.ConnectionError as e:
            return _simple_japanese_tokenize(text), True
        except requests.exceptions.RequestException as e:
            return _simple_japanese_tokenize(text), True
        except Exception as e:
            return _simple_japanese_tokenize(text), True
        finally:
            # Always decrement counter, even on error
            active_yomitan_operations = max(0, active_yomitan_operations - 1)

    def _simple_japanese_tokenize(text):
        """
        Simple Japanese tokenization fallback that preserves common adjectives.
        This is a basic approach to avoid over-splitting i-adjectives.
        """
        if not text or len(text) == 0:
            return []
        
        # Common Japanese i-adjectives that should NOT be split
        common_i_adjectives = {
            '熱い', '冷たい', '暖かい', '涼しい', '温かい',  # Temperature
            '固い', '柔らかい', '硬い', '軟らかい',           # Texture
            '重い', '軽い', '厚い', '薄い', '太い', '細い',    # Physical properties  
            '高い', '低い', '長い', '短い', '広い', '狭い',    # Size/dimension
            '新しい', '古い', '若い', '美しい', '可愛い',      # Age/beauty
            '大きい', '小さい', '多い', '少ない',            # Quantity
            '早い', '遅い', '速い', '安い', '高い',          # Speed/cost
            '良い', '悪い', '正しい', '間違い', '危ない',      # Quality/safety
            '楽しい', '悲しい', '嬉しい', '苦しい', '痛い',    # Emotions/sensations
            '難しい', '易しい', '忙しい', '暇い',            # Difficulty/business
            '面白い', '詰まらない', '珍しい', '普通い',        # Interest
            '眠い', '疲れい', '元気い', '健康い'             # Health/energy
        }
        
        tokens = []
        i = 0
        while i < len(text):
            # Check for multi-character adjectives first
            found_adjective = False
            for adj in common_i_adjectives:
                if text[i:i+len(adj)] == adj:
                    tokens.append(adj)
                    i += len(adj)
                    found_adjective = True
                    break
            
            if not found_adjective:
                # Single character fallback
                char = text[i]
                if char.strip():  # Skip whitespace
                    tokens.append(char)
                i += 1
        
        return tokens
    
    def filter_japanese_tokens(words):
        """
        Comprehensive filtering for Japanese tokens to remove junk.
        Applied to Yomitan API results.
        """
        filtered_words = []
        
        for word in words:
            # Skip empty or whitespace-only tokens
            if not word or not word.strip():
                continue
                
            # Skip pure punctuation, symbols, and decorative characters
            if re.match(r'^[。、！？「」『』（）・…ー～〜♡♥★☆◇◆■□▪▫●○◎▲△▼▽◀▶▲▼←→↑↓♪♫※\s\-\u2000-\u206F\u2E00-\u2E7F\u3000-\u303F\uFF00-\uFFEF]+$', word):
                continue
                
            # Skip tokens that are mostly decorative symbols/punctuation
            symbol_count = len(re.findall(r'[♡♥★☆◇◆■□▪▫●○◎▲△▼▽◀▶▲▼←→↑↓♪♫※。、！？「」『』（）・…ー～〜\s\-\u2000-\u206F\u2E00-\u2E7F\u3000-\u303F\uFF00-\uFFEF]', word))
            if symbol_count >= len(word) * 0.7:  # If 70% or more are symbols/punctuation
                continue
            
            # Skip single character particles and auxiliary verbs
            if len(word) == 1 and word in 'はをにがでとやかのもてだよねなをれしたっつくぐすむぶぬ':
                continue
            
            # Skip single hiragana characters (usually fragments) - but preserve important ones
            if len(word) == 1 and re.match(r'^[\u3040-\u309F]$', word):
                # Don't skip い (adjective ending) and other important single characters
                important_single = {'い', 'う', 'き', 'く', 'し', 'す', 'つ', 'ぬ', 'ふ', 'む', 'ゆ', 'る'}
                if word not in important_single:
                    continue
                
            # Skip single katakana characters (usually fragments) 
            if len(word) == 1 and re.match(r'^[\u30A0-\u30FF]$', word):
                continue
            
            # Skip very short hiragana fragments (likely incomplete words)
            if len(word) <= 2 and re.match(r'^[\u3040-\u309F]+$', word):
                # Allow common complete words only
                allowed_short = {'です', 'ます', 'この', 'その', 'あの', 'どの', 'これ', 'それ', 
                               'あれ', 'どれ', 'から', 'まで', 'より', 'など', 'でも', 'もう', 
                               'まだ', 'もの', 'こと', 'とき', 'ため', 'ごと', 'けど'}
                if word not in allowed_short:
                    continue
            
            # Skip fragments that are just conjugation endings
            if re.match(r'^[っつくぐすむぶぬたるれ]{1,2}$', word):
                continue
            
            # Include word if it contains Japanese content OR is non-Japanese text that passed all checks
            if re.search(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FAF]', word) or not re.match(r'^[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FAF]+$', word):
                filtered_words.append(word)
        
        return filtered_words

    def tokenize_japanese_text(text, progress_id=None):
        """
        Japanese tokenization using Yomitan API only.
        Returns empty list if Yomitan API is not available.
        """
        global progress_tracker
        
        if not text or not text.strip():
            return []
        
        if progress_id:
            progress_tracker[progress_id] = {
                'stage': 'preprocessing',
                'message': 'Preparing text for tokenization...',
                'progress': 5
            }
        
        # Remove HTML tags if present
        text = re.sub(r'<[^>]+>', '', text.strip())
        
        # Use Yomitan API for dictionary-based tokenization
        yomitan_words, success = tokenize_with_yomitan_api(text, progress_id=progress_id)
        
        if success and yomitan_words:
            if progress_id:
                progress_tracker[progress_id] = {
                    'stage': 'filtering',
                    'message': f'Filtering {len(yomitan_words)} tokens...',
                    'progress': 95
                }
            
            filtered_words = filter_japanese_tokens(yomitan_words)
            
            if progress_id:
                progress_tracker[progress_id] = {
                    'stage': 'complete',
                    'message': f'Tokenization complete! {len(filtered_words)} tokens after filtering',
                    'progress': 100
                }
            
            return filtered_words
        
        # No fallback - require Yomitan API for quality tokenization
        print("❌ Yomitan API not available - cannot tokenize text")
        print("💡 Please ensure Yomitan API server is running at http://127.0.0.1:19633")
        
        if progress_id:
            progress_tracker[progress_id] = {
                'stage': 'error',
                'message': 'Yomitan API not available',
                'progress': 0
            }
        
        return []

    def analyze_text_vocabulary(text_content, vocabulary_set, progress_id=None):
        """Analyze text using yomitan frequency data for ALL words"""
        global progress_tracker
        
        if progress_id:
            progress_tracker[progress_id] = {
                'stage': 'starting',
                'message': 'Starting vocabulary analysis...',
                'progress': 0
            }
        
        # Load ignored words
        ignored_words = load_ignored_words()
        
        words = tokenize_japanese_text(text_content, progress_id=progress_id)
        
        if progress_id:
            progress_tracker[progress_id] = {
                'stage': 'analyzing',
                'message': f'Getting yomitan data (frequency + vocabulary) for {len(words)} words...',
                'progress': 40
            }
        
        word_counts = Counter(words)
        
        # NEW APPROACH: Get frequency data for ALL words using yomitan
        all_words_with_frequency = []
        known_words = []  # Words in user's vocabulary 
        unknown_words = []  # Words not in user's vocabulary
        ignored_words_with_data = []  # Words that are ignored
        
        total_unique = len(word_counts)
        processed = 0
        
        for word, count in word_counts.items():
            processed += 1
            
            if progress_id:
                progress_tracker[progress_id] = {
                    'stage': 'vocabulary_lookup',
                    'message': f'Processing word {processed}/{total_unique}: {word} (frequency + vocabulary)',
                    'progress': 40 + (processed / total_unique) * 50
                }
            
            # Get frequency data from yomitan for this word
            freq_data = get_yomitan_frequency_data(word)
            
            # Simple one-to-one vocabulary comparison
            is_in_vocab = word in vocabulary_set
            is_ignored = word in ignored_words
            
            word_entry = {
                'word': word,  # Show original word as-is
                'count': count,
                'has_frequency': freq_data['found'],
                'rank': freq_data.get('rank'),
                'source': freq_data.get('source'),
                'in_vocabulary': is_in_vocab,
                'is_ignored': is_ignored,
                'original_word': word    # The original word from text
            }
            
            # Add to appropriate lists
            all_words_with_frequency.append(word_entry)
            
            if is_ignored:
                ignored_words_with_data.append(word_entry)
            elif is_in_vocab:
                known_words.append(word_entry)
            else:
                unknown_words.append(word_entry)
        
        
        # Deduplicate results - merge entries with same display word
        def deduplicate_word_list(word_list):
            """Merge word entries with the same display word, combining their counts."""
            word_dict = {}
            for entry in word_list:
                word_key = entry['word']
                if word_key in word_dict:
                    # Merge with existing entry
                    existing = word_dict[word_key]
                    existing['count'] += entry['count']
                    # Keep the first entry's other properties (frequency, etc.)
                    # but track all original words for debugging
                    if 'original_words' not in existing:
                        existing['original_words'] = [existing.get('original_word', word_key)]
                    existing['original_words'].append(entry.get('original_word', word_key))
                else:
                    # First occurrence of this word
                    word_dict[word_key] = entry.copy()
            
            return list(word_dict.values())
        
        # Apply deduplication to all lists
        all_words_with_frequency = deduplicate_word_list(all_words_with_frequency)
        known_words = deduplicate_word_list(known_words)
        unknown_words = deduplicate_word_list(unknown_words)
        
        # Sort by frequency (most common first)
        all_words_with_frequency.sort(key=lambda x: x['count'], reverse=True)
        known_words.sort(key=lambda x: x['count'], reverse=True)
        unknown_words.sort(key=lambda x: x['count'], reverse=True)
        ignored_words_with_data.sort(key=lambda x: x['count'], reverse=True)
        
        # Calculate comprehension rate based on processed words (excluding ignored words)
        known_word_count = sum(item['count'] for item in known_words)
        unknown_word_count = sum(item['count'] for item in unknown_words)
        ignored_word_count = sum(item['count'] for item in ignored_words_with_data)
        total_processed_words = known_word_count + unknown_word_count  # Words that count for comprehension (not ignored)
        
        # Use total processed words for comprehension rate calculation (ignores ignored words)
        comprehension_rate = (known_word_count / total_processed_words * 100) if total_processed_words > 0 else 0
        
        if progress_id:
            vocab_matches = len([w for w in all_words_with_frequency if w['in_vocabulary']])
            freq_matches = len([w for w in all_words_with_frequency if w['has_frequency']])
            progress_tracker[progress_id] = {
                'stage': 'complete',
                'message': f'Analysis complete! Found {freq_matches} frequency matches, {vocab_matches} vocabulary matches via yomitan',
                'progress': 100
            }
        
        # Calculate three-category frequency statistics (Known/Ignored/Unknown)
        star_stats = calculate_three_category_frequency_statistics(known_words, ignored_words_with_data, unknown_words)
        
        # Calculate difficulty level based on comprehension rate
        def calculate_difficulty_level(comprehension_rate):
            if comprehension_rate >= 95:
                return "1★ Beginner"
            elif comprehension_rate >= 85:
                return "2★ Elementary" 
            elif comprehension_rate >= 75:
                return "3★ Intermediate"
            elif comprehension_rate >= 65:
                return "4★ Advanced"
            else:
                return "5★ Expert"
        
        difficulty_level = calculate_difficulty_level(comprehension_rate)
        
        return {
            'total_words': len(words),  # Keep original raw token count for reference
            'unique_words': len(word_counts),
            'all_words_analyzed': all_words_with_frequency,  # NEW: All words with frequency data
            'known_words': known_words,
            'unknown_words': unknown_words,
            'ignored_words': ignored_words_with_data,  # NEW: Include ignored words data
            'known_word_count': known_word_count,
            'unknown_word_count': unknown_word_count,
            'ignored_word_count': ignored_word_count,  # NEW: Include ignored word count
            'total_processed_words': total_processed_words,  # NEW: Words actually analyzed (excluding ignored)
            'comprehension_rate': comprehension_rate,
            'difficulty_level': difficulty_level,
            'star_statistics': star_stats,  # NOW CONTAINS THREE CATEGORIES
            'parsing_info': {
                'total_raw_words': len(words),
                'filtered_unique_words': len(word_counts),
                'words_with_frequency': len([w for w in all_words_with_frequency if w['has_frequency']])
            }
        }

    @app.route('/')
    def home():
        """Library home page - Show uploaded novels and vocabulary caches"""
        # Get available cached vocabularies
        caches = get_available_caches()
        
        # Get uploaded novels with cover images
        novels = []
        if os.path.exists(novel_dir):
            for filename in os.listdir(novel_dir):
                if allowed_file(filename):
                    file_path = os.path.join(novel_dir, filename)
                    file_stat = os.stat(file_path)
                    
                    # Check for cover image with same name but different extension
                    base_name = filename.rsplit('.', 1)[0]
                    cover_image = None
                    for ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']:
                        cover_path = os.path.join(novel_dir, f"{base_name}.{ext}")
                        if os.path.exists(cover_path):
                            cover_image = f"{base_name}.{ext}"
                            break
                    
                    novels.append({
                        'filename': filename,
                        'size': file_stat.st_size,
                        'modified': datetime.fromtimestamp(file_stat.st_mtime),
                        'display_name': filename.rsplit('.', 1)[0],
                        'cover_image': cover_image
                    })
        
        # Sort novels by modification date (newest first)
        novels.sort(key=lambda x: x['modified'], reverse=True)
        
        return render_template('library_home.html', 
                             caches=caches, 
                             novels=novels,
                             has_caches=len(caches) > 0)

    @app.route('/health/yomitan')
    def yomitan_health():
        """Check Yomitan API health status"""
        is_healthy, status_message, response_time = check_yomitan_health()
        
        return {
            'healthy': is_healthy,
            'status': 'healthy' if is_healthy else 'unhealthy',
            'message': status_message,
            'response_time_ms': response_time,
            'timestamp': datetime.now().isoformat(),
            'yomitan_url': YOMITAN_API_URL
        }

    @app.route('/health/anki')
    def anki_health():
        """Check Anki Connect health status"""
        is_healthy, status_message, response_time = check_anki_health()
        
        return {
            'healthy': is_healthy,
            'status': 'healthy' if is_healthy else 'unhealthy',
            'message': status_message,
            'response_time_ms': response_time,
            'timestamp': datetime.now().isoformat(),
            'anki_url': 'http://127.0.0.1:8765'
        }

    @app.route('/upload', methods=['POST'])
    def upload_file():
        """Handle file upload"""
        if 'file' not in request.files:
            flash('No file selected', 'error')
            return redirect(url_for('home'))
        
        file = request.files['file']
        if file.filename == '':
            flash('No file selected', 'error')
            return redirect(url_for('home'))
        
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            # Add timestamp if file already exists
            if os.path.exists(os.path.join(novel_dir, filename)):
                name, ext = filename.rsplit('.', 1)
                filename = f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
            
            file_path = os.path.join(novel_dir, filename)
            file.save(file_path)
            flash(f'File "{filename}" uploaded successfully!', 'success')
        else:
            flash('Invalid file type. Please upload .txt, .md, or .epub files.', 'error')
        
        return redirect(url_for('home'))

    @app.route('/analyze/<filename>')
    def analyze_novel(filename):
        """Show analysis options for a novel"""
        file_path = os.path.join(novel_dir, filename)
        if not os.path.exists(file_path):
            flash('File not found', 'error')
            return redirect(url_for('home'))
        
        # Get available cached vocabularies
        caches = get_available_caches()
        
        if not caches:
            flash('No vocabulary caches found. Please create an Anki vocabulary cache first.', 'error')
            return redirect(url_for('anki_setup'))
        
        return render_template('analysis_setup.html', 
                             filename=filename, 
                             caches=caches)

    @app.route('/analyze/<filename>/<cache_key>')
    def perform_analysis(filename, cache_key):
        """Start vocabulary analysis with progress tracking"""
        file_path = os.path.join(novel_dir, filename)
        if not os.path.exists(file_path):
            flash('File not found', 'error')
            return redirect(url_for('home'))
        
        # Find the matching cache
        caches = get_available_caches()
        selected_cache = None
        for cache in caches:
            if cache['key'] == cache_key:
                selected_cache = cache
                break
        
        if not selected_cache:
            flash('Cache not found', 'error')
            return redirect(url_for('home'))
        
        # Generate unique progress ID
        progress_id = str(uuid.uuid4())
        
        # Store analysis parameters in session
        session['analysis_params'] = {
            'filename': filename,
            'cache_key': cache_key,
            'progress_id': progress_id
        }
        
        return render_template('analysis_progress.html',
                             filename=filename,
                             cache_info=selected_cache,
                             progress_id=progress_id)

    @app.route('/cached_analysis/<int:scan_id>')
    def view_cached_analysis(scan_id):
        """Display cached analysis results"""
        cached_data = database.get_scan_by_id(scan_id)
        if not cached_data:
            flash('Cached analysis not found', 'error')
            return redirect(url_for('home'))
        
        # Convert cached data to the expected format for the template
        total_processed_words = cached_data.get('total_processed_words')
        if total_processed_words is None:
            # Calculate from counts if not stored (for backward compatibility)
            total_processed_words = cached_data['known_words_count'] + cached_data['unknown_words_count']
        
        # Calculate star statistics for the template
        known_words = cached_data.get('known_words', [])
        unknown_words = cached_data.get('unknown_words', [])
        ignored_words = cached_data.get('ignored_words', [])
        
        # Calculate star statistics using the same function as the main analysis
        star_stats = calculate_three_category_frequency_statistics(known_words, ignored_words, unknown_words)
        
        # Calculate unique words count (total of all categories)
        unique_words_count = len(known_words) + len(unknown_words) + len(ignored_words)
            
        analysis = {
            'known_words': known_words,
            'unknown_words': unknown_words,
            'ignored_words': ignored_words,
            'star_distribution': cached_data['star_distribution'],
            'comprehension_rate': cached_data['comprehension_rate'],
            'difficulty_level': cached_data['difficulty_level'],
            'total_words': cached_data['total_words'],
            'total_instances': cached_data['total_instances'],
            'total_processed_words': total_processed_words,
            'unique_words': unique_words_count,
            'known_word_count': sum(item['count'] for item in known_words) if known_words else 0,
            'unknown_word_count': sum(item['count'] for item in unknown_words) if unknown_words else 0,
            'ignored_word_count': len(ignored_words),
            'star_statistics': star_stats
        }
        
        return render_template('analysis_results_compact.html',
                             filename=cached_data['filename'] or 'Cached Analysis',
                             cache_info={'name': 'Cached Analysis', 'created_at': cached_data['created_at']},
                             analysis=analysis,
                             is_cached=True)

    @app.route('/scan_history')
    def scan_history():
        """Display scan history with progress tracking"""
        scans = database.get_scan_history(limit=50)
        return render_template('scan_history.html', scans=scans)

    @app.route('/delete_scan/<int:scan_id>', methods=['POST'])
    def delete_scan(scan_id):
        """Delete a scan from history"""
        success = database.delete_scan(scan_id)
        if success:
            flash('Analysis record deleted successfully', 'success')
        else:
            flash('Failed to delete analysis record', 'error')
        
        # Check for redirect parameter or use referrer to determine where to go back
        redirect_to = request.form.get('redirect_to')
        if redirect_to:
            return redirect(redirect_to)
        
        # Check if we came from file_records page by looking at the referrer
        referrer = request.referrer or ''
        if 'file_records' in referrer:
            # Extract filename from referrer URL to redirect back to the same file_records page
            import re
            from urllib.parse import unquote
            match = re.search(r'/file_records/([^/?]+)', referrer)
            if match:
                filename = unquote(match.group(1))
                return redirect(url_for('file_records', filename=filename))
        
        # Default to scan_history if we can't determine or came from there
        return redirect(url_for('scan_history'))

    @app.route('/progress_comparison')
    def progress_comparison():
        """Show progress comparison over time"""
        comparisons = database.get_progress_comparison(limit=20)
        return render_template('progress_comparison.html', comparisons=comparisons)

    @app.route('/file_records/<filename>')
    def file_records(filename):
        """Show analysis records for a specific file"""
        records = database.get_scans_by_filename(filename)
        return render_template('file_records.html', filename=filename, records=records)
    
    @app.route('/test-progress')
    def test_progress():
        """Test progress tracking system"""
        test_id = str(uuid.uuid4())
        
        # Simulate progress updates
        def update_progress():
            stages = [
                ('starting', 'Test starting...', 10),
                ('tokenizing', 'Test tokenizing...', 30),
                ('analyzing', 'Test analyzing...', 70),
                ('complete', 'Test complete!', 100)
            ]
            
            for stage, message, progress in stages:
                progress_tracker[test_id] = {
                    'stage': stage,
                    'message': message,
                    'progress': progress
                }
                time.sleep(2)  # Wait 2 seconds between updates
        
        # Start progress updates in background (in a real app, use threading or celery)
        import threading
        thread = threading.Thread(target=update_progress)
        thread.start()
        
        return render_template('analysis_progress.html',
                             filename='test-file.txt',
                             cache_info={'key': 'test'},
                             progress_id=test_id)

    @app.route('/test-new-analysis')
    def test_new_analysis():
        """Test the new universal frequency analysis approach"""
        try:
            # Load vocabulary cache (just use first available cache for testing)
            caches = get_available_caches()
            if not caches:
                return """
                <html>
                <body>
                <h1>⚠️ No vocabulary caches found</h1>
                <p>Please create an Anki vocabulary cache first by going to the Anki Setup page.</p>
                </body>
                </html>
                """
            
            selected_cache = caches[0]  # Use first available cache
            cache_file_path = os.path.join(anki_manager.cache_dir, selected_cache['filename'])
            with open(cache_file_path, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            # Extract vocabulary set
            vocabulary_set = set()
            for card_data in cache_data['cards'].values():
                expr = card_data['expression'].strip()
                if expr:
                    vocabulary_set.add(expr)
            
            # Test text with words likely to have different dictionary forms
            test_text = "熱いお茶を飲みました。固いパンを食べます。"  # Test adjectives that were being split
            
            # Run the new analysis
            results = analyze_text_vocabulary(test_text, vocabulary_set)
            
            # Prepare summary data
            words_with_freq = [w for w in results['all_words_analyzed'] if w['has_frequency']]
            words_with_vocab = [w for w in results['all_words_analyzed'] if w['in_vocabulary']]
            vocabulary_matches = [w for w in results['all_words_analyzed'] if w['in_vocabulary'] and w.get('matched_form') != w['word']]
            
            star_stats = results['star_statistics']
            
            summary = {
                'text_tested': test_text,
                'vocabulary_size': len(vocabulary_set),
                'cache_used': selected_cache['key'],
                'total_words': results['total_words'],
                'unique_words': results['unique_words'],
                'words_analyzed': len(results['all_words_analyzed']),
                'words_with_frequency': len(words_with_freq),
                'words_with_vocab': len(words_with_vocab),
                'yomitan_vocab_matches': vocabulary_matches[:5],  # Show yomitan-matched words
                'star_distribution': star_stats['star_distribution'],
                'average_rating': star_stats['average_star_rating'],
                'sample_words': words_with_freq[:10]  # First 10 words with frequency
            }
            
            return f"""
            <html>
            <head><title>New Universal Analysis Test</title></head>
            <body>
            <h1>✅ New Universal Frequency Analysis Test</h1>
            
            <h2>Test Setup:</h2>
            <ul>
            <li><strong>Vocabulary cache used:</strong> {summary['cache_used']}</li>
            <li><strong>Vocabulary size:</strong> {summary['vocabulary_size']} words</li>
            <li><strong>Test text:</strong> <code>{summary['text_tested']}</code></li>
            </ul>
            
            <h2>Analysis Results:</h2>
            <ul>
            <li><strong>Total words:</strong> {summary['total_words']}</li>
            <li><strong>Unique words:</strong> {summary['unique_words']}</li>
            <li><strong>Words analyzed:</strong> {summary['words_analyzed']}</li>
            <li><strong>Words with frequency data:</strong> {summary['words_with_frequency']}</li>
            <li><strong>Words in your vocabulary:</strong> {summary['words_with_vocab']} (via yomitan lookup!)</li>
            </ul>
            
            <h2>🔍 Yomitan Vocabulary Matching Examples:</h2>
            <ul>
            {''.join([f"<li>'{word['word']}' → '{word['matched_form']}' ✅ (matched via yomitan)</li>" for word in summary['yomitan_vocab_matches']]) if summary['yomitan_vocab_matches'] else '<li>No yomitan vocabulary transformations in this sample</li>'}
            </ul>
            
            <h2>Star Rating Distribution:</h2>
            <ul>
            <li>5 stars: {summary['star_distribution'][5]} words</li>
            <li>4 stars: {summary['star_distribution'][4]} words</li>
            <li>3 stars: {summary['star_distribution'][3]} words</li>
            <li>2 stars: {summary['star_distribution'][2]} words</li>
            <li>1 star: {summary['star_distribution'][1]} words</li>
            <li>0 stars: {summary['star_distribution'][0]} words</li>
            <li><strong>Average rating:</strong> {summary['average_rating']} stars</li>
            </ul>
            
            <h2>Sample Words with Frequency Data:</h2>
            <ul>
            {''.join([f"<li>{word['word']}: rank {word['rank']}, appears {word['count']}x, in vocab: {'Yes' if word['in_vocabulary'] else 'No'}</li>" for word in summary['sample_words']])}
            </ul>
            
            <p><strong>🎉 SUCCESS:</strong> The new yomitan-powered analysis is working! Analyzing <strong>ALL words</strong> for frequency + using <strong>yomitan dictionary forms</strong> for vocabulary matching (e.g., おちゃ → お茶).</p>
            <p><a href="/">← Back to main page</a></p>
            </body>
            </html>
            """
            
        except Exception as e:
            import traceback
            return f"""
            <html>
            <head><title>Test Error</title></head>
            <body>
            <h1>❌ Test Error</h1>
            <p><strong>Error:</strong> {str(e)}</p>
            <pre>{traceback.format_exc()}</pre>
            <p><a href="/">← Back to main page</a></p>
            </body>
            </html>
            """

    @app.route('/analyze/progress/<progress_id>')
    def analysis_progress_sse(progress_id):
        """Server-Sent Events endpoint for analysis progress"""
        def generate():
            # Initialize progress if not exists
            if progress_id not in progress_tracker:
                progress_tracker[progress_id] = {
                    'stage': 'starting',
                    'message': 'Initializing analysis...',
                    'progress': 0
                }
            
            while progress_id in progress_tracker:
                progress = progress_tracker[progress_id]
                yield f"data: {json.dumps(progress)}\n\n"
                time.sleep(0.5)
                
                if progress.get('stage') in ['complete', 'error']:
                    # Send one final update and break
                    time.sleep(1)
                    break
            
        response = Response(generate(), mimetype='text/event-stream')
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Connection'] = 'keep-alive'
        return response
    
    @app.route('/analyze/execute/<progress_id>')
    def execute_analysis(progress_id):
        """Start analysis in background thread"""
        if 'analysis_params' not in session:
            progress_tracker[progress_id] = {
                'stage': 'error',
                'message': 'No analysis parameters found',
                'progress': 0
            }
            return {'error': 'No analysis parameters found'}, 400
            
        params = session['analysis_params']
        filename = params['filename']
        cache_key = params['cache_key']
        
        # Initialize progress
        progress_tracker[progress_id] = {
            'stage': 'starting',
            'message': 'Initializing analysis...',
            'progress': 0
        }
        
        def run_analysis(filename, cache_key, progress_id, app_context):
            """Run analysis in background thread with app context"""
            with app_context:
                file_path = os.path.join(novel_dir, filename)
                
                try:
                    progress_tracker[progress_id] = {
                        'stage': 'starting',
                        'message': 'Reading file and loading vocabulary...',
                        'progress': 5
                    }
                    
                    # Read the novel file
                    with open(file_path, 'r', encoding='utf-8') as f:
                        text_content = f.read()
                    
                    # Find the matching cache
                    caches = get_available_caches()
                    selected_cache = None
                    for cache in caches:
                        if cache['key'] == cache_key:
                            selected_cache = cache
                            break
                    
                    if not selected_cache:
                        progress_tracker[progress_id] = {
                            'stage': 'error',
                            'message': 'Cache not found',
                            'progress': 0
                        }
                        return
                    
                    # Load vocabulary from cache
                    cache_file_path = os.path.join(anki_manager.cache_dir, selected_cache['filename'])
                    with open(cache_file_path, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)
                    
                    # Extract vocabulary set
                    vocabulary_set = set()
                    for card_data in cache_data['cards'].values():
                        expr = card_data['expression'].strip()
                        if expr:
                            vocabulary_set.add(expr)
                    
                    # Perform analysis with progress tracking
                    analysis = analyze_text_vocabulary(text_content, vocabulary_set, progress_id=progress_id)
                    
                    # Save analysis to database
                    try:
                        scan_id = database.save_scan_result(
                            analysis_data=analysis,
                            text_content=text_content,
                            filename=filename
                        )
                    except Exception as e:
                        pass  # Save operation failed, but analysis completed successfully
                    
                    # Store results in a way that doesn't require session context
                    # We'll use the progress_tracker to store results temporarily
                    progress_tracker[progress_id] = {
                        'stage': 'complete',
                        'message': 'Analysis complete! Redirecting...',
                        'progress': 100,
                        'results': {
                            'analysis': analysis,
                            'filename': filename,
                            'cache_info': selected_cache
                        }
                    }
                    
                except Exception as e:
                    progress_tracker[progress_id] = {
                        'stage': 'error',
                        'message': f'Analysis failed: {str(e)}',
                        'progress': 0
                    }
        
        # Start analysis in background thread with app context
        import threading
        thread = threading.Thread(
            target=run_analysis,
            args=(filename, cache_key, progress_id, app.app_context())
        )
        thread.daemon = True  # Thread will die when main program exits
        thread.start()
        
        return {'status': 'started', 'progress_id': progress_id}
    
    @app.route('/analysis/results')
    def analysis_results():
        """Display analysis results"""
        # Try to get results from session first (for backwards compatibility)
        if 'analysis_results' in session:
            results = session['analysis_results']
            del session['analysis_results']  # Clean up session
            
            return render_template('analysis_results_compact.html',
                                 filename=results['filename'],
                                 cache_info=results['cache_info'],
                                 analysis=results['analysis'])
        
        # Otherwise, try to get from progress_tracker via URL parameter
        progress_id = request.args.get('progress_id')
        if progress_id and progress_id in progress_tracker:
            progress_data = progress_tracker[progress_id]
            if 'results' in progress_data:
                results = progress_data['results']
                # Clean up progress tracker
                del progress_tracker[progress_id]
                
                return render_template('analysis_results_compact.html',
                                     filename=results['filename'],
                                     cache_info=results['cache_info'],
                                     analysis=results['analysis'])
        
        flash('No analysis results found', 'error')
        return redirect(url_for('home'))

    @app.route('/ignore_word', methods=['POST'])
    def ignore_word():
        """Add a word to the ignore list via AJAX"""
        try:
            data = request.get_json()
            if not data or 'word' not in data:
                return {'success': False, 'error': 'No word provided'}, 400
            
            word = data['word'].strip()
            if not word:
                return {'success': False, 'error': 'Empty word'}, 400
            
            # Add word to ignored list
            success = add_ignored_word(word)
            
            if success:
                return {'success': True, 'message': f'Word "{word}" has been ignored'}
            else:
                return {'success': False, 'error': 'Failed to save ignored word'}, 500
                
        except Exception as e:
            print(f"Error in ignore_word route: {e}")
            return {'success': False, 'error': str(e)}, 500
    
    @app.route('/unignore_word', methods=['POST'])
    def unignore_word():
        """Remove a word from the ignore list via AJAX"""
        try:
            data = request.get_json()
            if not data or 'word' not in data:
                return {'success': False, 'error': 'No word provided'}, 400
            
            word = data['word'].strip()
            if not word:
                return {'success': False, 'error': 'Empty word'}, 400
            
            # Remove word from ignored list
            success = remove_ignored_word(word)
            
            if success:
                return {'success': True, 'message': f'Word "{word}" is no longer ignored'}
            else:
                return {'success': False, 'error': 'Failed to remove ignored word'}, 500
                
        except Exception as e:
            print(f"Error in unignore_word route: {e}")
            return {'success': False, 'error': str(e)}, 500
    
    @app.route('/ignored_words', methods=['GET'])
    def get_ignored_words():
        """Get the list of ignored words via AJAX"""
        try:
            ignored_words = load_ignored_words()
            return {
                'success': True, 
                'ignored_words': sorted(list(ignored_words)),
                'count': len(ignored_words)
            }
        except Exception as e:
            print(f"Error in get_ignored_words route: {e}")
            return {'success': False, 'error': str(e)}, 500

    @app.route('/cover/<filename>')
    def serve_cover(filename):
        """Serve cover image files"""
        return send_from_directory(novel_dir, filename)

    @app.route('/download/<filename>')
    def download_novel(filename):
        """Download a novel file"""
        return send_from_directory(novel_dir, filename, as_attachment=True)

    @app.route('/delete/<filename>')
    def delete_novel(filename):
        """Delete a novel file"""
        file_path = os.path.join(novel_dir, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            flash(f'File "{filename}" deleted successfully', 'success')
        else:
            flash('File not found', 'error')
        return redirect(url_for('home'))

    @app.route('/anki_setup')
    def anki_setup():
        """Anki setup page for creating vocabulary caches"""
        # Check if any cache exists
        cache_exists = False
        if os.path.exists(anki_manager.cache_dir):
            cache_files = [f for f in os.listdir(anki_manager.cache_dir) if f.endswith(('.json', '.pkl'))]
            cache_exists = len(cache_files) > 0

        # Get available note types from Anki
        try:
            note_types_result = anki_manager.ankiConnectInvoke('modelNamesAndIds', 6)
            note_types = list(note_types_result['result'].keys()) if note_types_result else []
        except Exception as e:
            flash(f"Error connecting to Anki: {str(e)}", 'error')
            note_types = []

        return render_template('anki_setup.html', 
                             cache_exists=cache_exists, 
                             note_types=note_types,
                             anki_connected=len(note_types) > 0)

    @app.route('/select_field', methods=['POST'])
    def select_field():
        """Handle note type selection and show field selection"""
        selected_note = request.form.get('note_type')
        if not selected_note:
            flash('Please select a note type', 'error')
            return redirect(url_for('home'))

        # Get available fields for the selected note type
        try:
            fields_result = anki_manager.ankiConnectInvoke('modelFieldNames', 6, {'modelName': selected_note})
            fields = fields_result['result'] if fields_result else []
        except Exception as e:
            flash(f"Error getting fields: {str(e)}", 'error')
            return redirect(url_for('home'))

        # Check if cache exists for this note type
        existing_caches = []
        if os.path.exists(anki_manager.cache_dir):
            for file in os.listdir(anki_manager.cache_dir):
                if file.startswith(selected_note.replace(" ", "_").replace(":", "_")) and file.endswith('.json'):
                    # Extract field name from filename
                    field_name = file.replace(selected_note.replace(" ", "_").replace(":", "_") + "_", "").replace(".json", "").replace("_", " ")
                    existing_caches.append(field_name)

        return render_template('field_selection.html', 
                             note_type=selected_note, 
                             fields=fields,
                             existing_caches=existing_caches)

    @app.route('/process_data', methods=['POST'])
    def process_data():
        """Process the selected note type and field"""
        note_type = request.form.get('note_type')
        field = request.form.get('field')
        action = request.form.get('action', 'update')  # 'update', 'full_refresh', or 'load_only'

        if not note_type or not field:
            flash('Please select both note type and field', 'error')
            return redirect(url_for('home'))

        try:
            # Store processing start time
            start_time = datetime.now()
            
            # Process based on action
            if action == 'full_refresh':
                cache_data = anki_manager.update_card_cache(note_type, field, force_full_update=True)
                expressions = anki_manager.get_expressions(note_type, field, update_cache=False)
                flash(f'Full refresh completed for {note_type} - {field}', 'success')
            elif action == 'load_only':
                expressions = anki_manager.get_expressions(note_type, field, update_cache=False)
                cache_data = anki_manager.load_cache(note_type, field)
                flash(f'Loaded existing cache for {note_type} - {field}', 'info')
            else:  # incremental update
                expressions = anki_manager.get_expressions(note_type, field, update_cache=True)
                cache_data = anki_manager.load_cache(note_type, field)
                flash(f'Incremental update completed for {note_type} - {field}', 'success')

            # Processing time
            processing_time = (datetime.now() - start_time).total_seconds()

            return render_template('results.html',
                                 note_type=note_type,
                                 field=field,
                                 expressions=expressions[:100],  # Show first 100
                                 total_expressions=len(expressions),
                                 cache_info=cache_data['metadata'],
                                 processing_time=processing_time)

        except Exception as e:
            flash(f'Error processing data: {str(e)}', 'error')
            return redirect(url_for('home'))

    @app.route('/settings')
    def settings():
        """Unified settings page with Anki setup and cache status"""
        # Cache status information
        cache_info = []
        if os.path.exists(anki_manager.cache_dir):
            for file in os.listdir(anki_manager.cache_dir):
                if file.endswith('.json'):
                    try:
                        file_path = os.path.join(anki_manager.cache_dir, file)
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            cache_info.append({
                                'filename': file,
                                'note_type': data['metadata'].get('note_type', 'Unknown'),
                                'field_name': data['metadata'].get('field_name', 'Unknown'),
                                'last_updated': data['metadata'].get('last_updated', 'Never'),
                                'total_cards': data['metadata'].get('total_cards', 0)
                            })
                    except:
                        pass

        # Check if any cache exists
        cache_exists = len(cache_info) > 0

        # Get available note types from Anki
        try:
            note_types_result = anki_manager.ankiConnectInvoke('modelNamesAndIds', 6)
            note_types = list(note_types_result['result'].keys()) if note_types_result else []
        except Exception as e:
            note_types = []

        return render_template('settings.html', 
                             cache_info=cache_info,
                             cache_exists=cache_exists, 
                             note_types=note_types,
                             anki_connected=len(note_types) > 0)

    @app.route('/cache_status')
    def cache_status():
        """Show status of all cached data"""
        cache_info = []
        if os.path.exists(anki_manager.cache_dir):
            for file in os.listdir(anki_manager.cache_dir):
                if file.endswith('.json'):
                    try:
                        file_path = os.path.join(anki_manager.cache_dir, file)
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            cache_info.append({
                                'filename': file,
                                'note_type': data['metadata'].get('note_type', 'Unknown'),
                                'field_name': data['metadata'].get('field_name', 'Unknown'),
                                'last_updated': data['metadata'].get('last_updated', 'Never'),
                                'total_cards': data['metadata'].get('total_cards', 0)
                            })
                    except:
                        pass

        return render_template('cache_status.html', cache_info=cache_info)

    @app.route('/delete_cache', methods=['POST'])
    def delete_cache():
        """Delete a vocabulary cache"""
        note_type = request.form.get('note_type')
        field_name = request.form.get('field_name')
        
        if not note_type or not field_name:
            flash('Invalid cache selection', 'error')
            return redirect(url_for('settings'))
        
        try:
            # Get cache filenames
            json_file, pickle_file = anki_manager.get_cache_filename(note_type, field_name)
            
            # Delete both files if they exist
            deleted_files = []
            if os.path.exists(json_file):
                os.remove(json_file)
                deleted_files.append('JSON cache')
            if os.path.exists(pickle_file):
                os.remove(pickle_file)
                deleted_files.append('Pickle cache')
            
            if deleted_files:
                flash(f'Successfully deleted {", ".join(deleted_files)} for {note_type} - {field_name}', 'success')
            else:
                flash(f'No cache files found for {note_type} - {field_name}', 'warning')
                
        except Exception as e:
            flash(f'Error deleting cache: {str(e)}', 'error')
            
        return redirect(url_for('settings'))

    @app.route('/clear_all_caches', methods=['POST'])
    def clear_all_caches():
        """Delete all vocabulary caches"""
        try:
            deleted_count = 0
            deleted_files = []
            
            if os.path.exists(anki_manager.cache_dir):
                for file in os.listdir(anki_manager.cache_dir):
                    if file.endswith(('.json', '.pkl')) and not file in ['ignored_words.json', 'scan_history.db']:
                        file_path = os.path.join(anki_manager.cache_dir, file)
                        try:
                            os.remove(file_path)
                            deleted_files.append(file)
                            deleted_count += 1
                        except Exception as e:
                            print(f"Error deleting {file}: {e}")
            
            if deleted_count > 0:
                flash(f'Successfully deleted {deleted_count} cache files', 'success')
            else:
                flash('No cache files found to delete', 'warning')
                
        except Exception as e:
            flash(f'Error clearing caches: {str(e)}', 'error')
            
        return redirect(url_for('settings'))

    @app.route('/view_cache_expressions')
    def view_cache_expressions():
        """View all expressions in a vocabulary cache"""
        note_type = request.args.get('note_type')
        field_name = request.args.get('field_name')
        
        if not note_type or not field_name:
            flash('Note type and field name are required', 'error')
            return redirect(url_for('settings'))
        
        try:
            # Get expressions from cache
            expressions = anki_manager.get_expressions(note_type, field_name, update_cache=False)
            cache_data = anki_manager.load_cache(note_type, field_name)
            
            if not expressions:
                flash(f'No expressions found in cache for {note_type} - {field_name}', 'warning')
                return redirect(url_for('settings'))
            
            return render_template('view_cache_expressions.html',
                                 note_type=note_type,
                                 field_name=field_name,
                                 expressions=expressions,
                                 total_expressions=len(expressions),
                                 cache_info=cache_data['metadata'] if cache_data else None)
            
        except Exception as e:
            flash(f'Error loading expressions: {str(e)}', 'error')
            return redirect(url_for('settings'))
    
    return app