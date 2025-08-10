import os
import logging
import random
import sqlite3
import json
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash
from flask_session import Session
import google.generativeai as genai
from deep_translator import GoogleTranslator, exceptions
from langdetect import detect, DetectorFactory
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

# Try to import optional dependencies
try:
    import markdown
    MARKDOWN_AVAILABLE = True
except ImportError:
    MARKDOWN_AVAILABLE = False

try:
    from textblob import TextBlob
    SENTIMENT_AVAILABLE = True
except ImportError:
    SENTIMENT_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
DetectorFactory.seed = 0
load_dotenv()

# Initialize Flask app
app = Flask(__name__, template_folder='Templates')

# Configure session
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
if not app.config["SECRET_KEY"]:
    app.logger.warning("SECRET_KEY not set. Using fallback for development.")
    app.config["SECRET_KEY"] = "dev_fallback_key_change_in_production"

app.config["SESSION_TYPE"] = "filesystem"
if not os.path.exists("./flask_session"):
    os.makedirs("./flask_session")
app.config["SESSION_FILE_DIR"] = "./flask_session"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True

Session(app)

# Configure Gemini AI
GEMINI_API_KEY = "AIzaSyCHaB3HFw0wwKldiAcyxeuVV4Fp-QOdWZ8"
if not GEMINI_API_KEY:
    app.logger.error("GEMINI_API_KEY not found in environment variables.")
    raise ValueError("GEMINI_API_KEY is required.")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Mental health resources
CRISIS_RESOURCES = {
    "pakistan": ["Emergency: 15 or 1122", "Umang Mental Health: 0317-6367833"],
    "international": ["Crisis Text Line: Text HOME to 741741"],
    "national": ["National Crisis Helpline: 042-35761999"]
}

# Database initialization
def init_db():
    """Initialize SQLite database with required tables"""
    conn = sqlite3.connect('dil_azaad.db')
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    ''')
    
    # Chat history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT NOT NULL,
            response TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Sentiment tracking table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sentiment_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_message TEXT,
            sentiment TEXT,
            confidence_score REAL DEFAULT 0.5,
            emotions_detected TEXT,
            crisis_flag INTEGER DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # User streaks table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_streaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            current_streak INTEGER DEFAULT 0,
            longest_streak INTEGER DEFAULT 0,
            last_checkin DATE,
            total_checkins INTEGER DEFAULT 0,
            streak_history TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    conn.close()

# Database helper functions
def get_db_connection():
    """Get database connection"""
    conn = sqlite3.connect('dil_azaad.db')
    conn.row_factory = sqlite3.Row
    return conn

def get_user_by_username(username):
    """Get user by username"""
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    return user

def create_user(username, email, password):
    """Create new user"""
    conn = get_db_connection()
    password_hash = generate_password_hash(password)
    try:
        cursor = conn.execute(
            'INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)',
            (username, email, password_hash)
        )
        user_id = cursor.lastrowid
        # Initialize streak data
        conn.execute(
            'INSERT INTO user_streaks (user_id, streak_history) VALUES (?, ?)',
            (user_id, '{}')
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def save_chat_message(user_id, message, response):
    """Save chat message to database"""
    if not user_id:
        return
    
    try:
        conn = get_db_connection()
        # Clean HTML from response for database storage
        clean_response = response.replace('<div style="', '').replace('</div>', '').split('>')[1].split('<')[0] if '<div' in response else response
        conn.execute(
            'INSERT INTO chat_history (user_id, message, response) VALUES (?, ?, ?)',
            (user_id, message, clean_response)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        app.logger.error(f"Error saving chat message: {e}")

def get_user_chat_history(user_id, limit=50):
    """Get user's chat history"""
    conn = get_db_connection()
    messages = conn.execute(
        'SELECT message, response, timestamp FROM chat_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?',
        (user_id, limit)
    ).fetchall()
    conn.close()
    return list(reversed(messages))

def analyze_sentiment_lightweight(text):
    """Simple sentiment analysis for crisis detection"""
    text_lower = text.lower()
    
    crisis_keywords = ['suicide', 'kill myself', 'end it all', 'want to die', 'harm myself', 
                      'better off dead', 'no point living', 'خودکشی']
    
    crisis_detected = any(keyword in text_lower for keyword in crisis_keywords)
    
    # Basic sentiment scoring
    negative_words = ['sad', 'depressed', 'hopeless', 'anxious', 'worried', 'stressed']
    positive_words = ['happy', 'good', 'better', 'grateful', 'thankful']
    
    negative_count = sum(1 for word in negative_words if word in text_lower)
    positive_count = sum(1 for word in positive_words if word in text_lower)
    
    if crisis_detected:
        sentiment = 'severe_negative'
    elif negative_count > positive_count:
        sentiment = 'negative'
    elif positive_count > negative_count:
        sentiment = 'positive'
    else:
        sentiment = 'neutral'
    
    return {
        'sentiment': sentiment,
        'crisis_detected': crisis_detected,
        'confidence': 0.8 if crisis_detected else 0.6
    }

def save_mood_tracking(user_id, sentiment_data, user_message):
    """Save mood tracking data"""
    if not user_id:
        return False
    
    try:
        conn = get_db_connection()
        conn.execute("""
            INSERT INTO sentiment_tracking 
            (user_id, sentiment, confidence_score, crisis_flag, user_message)
            VALUES (?, ?, ?, ?, ?)
        """, (
            user_id,
            sentiment_data['sentiment'],
            sentiment_data['confidence'],
            1 if sentiment_data['crisis_detected'] else 0,
            user_message[:500]
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        app.logger.error(f"Error saving mood tracking: {e}")
        return False

def get_streak_data(user_id):
    """Get streak data for a user"""
    try:
        conn = get_db_connection()
        
        # Get or create streak record
        streak_row = conn.execute(
            'SELECT * FROM user_streaks WHERE user_id = ?', (user_id,)
        ).fetchone()
        
        if not streak_row:
            # Initialize streak record
            conn.execute(
                'INSERT INTO user_streaks (user_id, streak_history) VALUES (?, ?)',
                (user_id, '{}')
            )
            conn.commit()
            streak_row = conn.execute(
                'SELECT * FROM user_streaks WHERE user_id = ?', (user_id,)
            ).fetchone()
        
        # Parse streak history
        try:
            streak_history = json.loads(streak_row['streak_history'] or '{}')
        except:
            streak_history = {}
        
        # Generate last 30 days data for calendar
        graph_data = []
        today = date.today()
        
        for i in range(30):
            check_date = today - timedelta(days=29-i)
            date_str = check_date.isoformat()
            
            graph_data.append({
                'date': date_str,
                'day': check_date.day,
                'month': check_date.strftime('%b'),
                'checked_in': streak_history.get(date_str, False)
            })
        
        streak_data = {
            'current_streak': streak_row['current_streak'] or 0,
            'longest_streak': streak_row['longest_streak'] or 0,
            'total_checkins': streak_row['total_checkins'] or 0,
            'last_checkin': streak_row['last_checkin']
        }
        
        conn.close()
        return streak_data, graph_data
        
    except Exception as e:
        app.logger.error(f"Error getting streak data: {e}")
        return {
            'current_streak': 0,
            'longest_streak': 0,
            'total_checkins': 0,
            'last_checkin': None
        }, []

# Initialize database on startup
init_db()

# Routes
@app.route("/")
def welcome():
    """Welcome page"""
    return render_template('welcome.html')

@app.route("/login", methods=["GET", "POST"])
def login():
    """User login"""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        if not username or not password:
            flash("Username and password are required", "error")
            return render_template('login.html')
        
        user = get_user_by_username(username)
        
        if user and check_password_hash(user['password_hash'], password):
            session["user"] = username
            session["user_id"] = user['id']
            
            # Update last login
            conn = get_db_connection()
            conn.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (user['id'],))
            conn.commit()
            conn.close()
            
            flash("Login successful!", "success")
            return redirect(url_for("chat_page"))
        else:
            flash("Invalid username or password", "error")
    
    return render_template('login.html')

@app.route("/register", methods=["GET", "POST"])
def register():
    """User registration"""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        email = request.form.get("email")
        
        if not username or not password or not email:
            flash("All fields are required", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters", "error")
        else:
            if create_user(username, email, password):
                flash("Registration successful! Please login.", "success")
                return redirect(url_for("login"))
            else:
                flash("Username or email already exists", "error")
    
    return render_template('register.html')

@app.route("/logout")
def logout():
    """User logout"""
    session.pop("user", None)
    session.pop("user_id", None)
    flash("Logged out successfully", "success")
    return redirect(url_for("welcome"))

@app.route("/chat")
def chat_page():
    """Chat page for logged-in users"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user_id = session.get("user_id")
    chat_history = get_user_chat_history(user_id) if user_id else []
    
    return render_template('chat.html', chat_history=chat_history)

@app.route("/guest")
def guest_chat():
    """Guest chat page"""
    return render_template('guest_chat.html')

@app.route("/chat", methods=["POST"])
def chat():
    """Main chat endpoint"""
    data = request.get_json()
    user_input = data.get("user_input", "").strip() or data.get("message", "").strip()
    
    if not user_input:
        return jsonify({"error": "Message is required"}), 400

    # Analyze sentiment for crisis detection
    sentiment_data = analyze_sentiment_lightweight(user_input)
    
    # Save mood tracking for registered users
    if "user_id" in session:
        save_mood_tracking(session["user_id"], sentiment_data, user_input)

    # Crisis intervention
    if sentiment_data.get('crisis_detected', False):
        crisis_response = '''
        <div style="background: #ffe6e6; border-left: 4px solid #ff4444; padding: 15px; border-radius: 8px; margin: 10px 0;">
            <h3 style="color: #cc0000; margin-bottom: 10px;">🚨 Immediate Support Needed</h3>
            <p style="margin: 10px 0;">I'm concerned about what you're sharing. Your life has value.</p>
            <p style="margin: 10px 0;"><strong>Emergency Help:</strong></p>
            <ul style="margin: 10px 0; padding-left: 20px;">
                <li>Pakistan: Emergency 15 or 1122</li>
                <li>Mental Health: Umang Helpline 0317-6367833</li>
                <li>Crisis Text Line: Text HOME to 741741</li>
            </ul>
            <p style="margin: 10px 0;">Please reach out to a human counselor immediately.</p>
        </div>
        '''
        
        if "user_id" in session:
            save_chat_message(session["user_id"], user_input, "Crisis intervention resources provided")
        
        return jsonify({"response": crisis_response})

    # Regular AI response using Gemini
    try:
        prompt = f"""You are a mental health support chatbot. Provide empathetic, helpful responses.

        Guidelines:
        - Be supportive and understanding
        - Keep responses concise (1-3 sentences)
        - No religious content
        - Offer practical advice when appropriate
        - Encourage professional help for serious issues
        
        User message: "{user_input}"
        
        Respond helpfully and empathetically:"""
        
        response = model.generate_content(prompt)
        ai_response = response.text.strip()
        
        formatted_response = f'''
        <div style="background: #f8fff8; border-left: 4px solid #10B981; padding: 15px; border-radius: 8px; margin: 10px 0;">
            <p style="color: #2c3e50; margin: 0; line-height: 1.5;">{ai_response}</p>
        </div>
        '''
        
        # Save chat for registered users
        if "user_id" in session:
            save_chat_message(session["user_id"], user_input, ai_response)
        
        return jsonify({"response": formatted_response})
        
    except Exception as e:
        app.logger.error(f"Error in chat function: {e}")
        fallback_response = '''
        <div style="background: #fff8e7; border-left: 4px solid #f39c12; padding: 15px; border-radius: 8px; margin: 10px 0;">
            <p style="color: #2c3e50; margin: 0; line-height: 1.5;">I'm here to help. Could you tell me more about how you're feeling?</p>
        </div>
        '''
        
        if "user_id" in session:
            save_chat_message(session["user_id"], user_input, "I'm here to help. Could you tell me more about how you're feeling?")
        
        return jsonify({"response": fallback_response})

@app.route("/streak")
def streak():
    """Streak tracking page"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user_id = session.get("user_id")
    
    # Get streak data
    streak_data, graph_data = get_streak_data(user_id)
    today = date.today().isoformat()
    
    return render_template('streak.html', 
                         streak_data=streak_data, 
                         graph_data=graph_data, 
                         today=today)

@app.route("/get_streak_data", methods=["GET"])
def get_streak_data_route():
    """API endpoint to get streak data"""
    if "user_id" not in session:
        return jsonify({"error": "User not logged in"}), 401
    
    user_id = session.get("user_id")
    streak_data, _ = get_streak_data(user_id)
    
    return jsonify({
        "current_streak": streak_data['current_streak'],
        "longest_streak": streak_data['longest_streak'],
        "total_checkins": streak_data['total_checkins'],
        "last_checkin": streak_data['last_checkin']
    })

@app.route("/checkin", methods=["POST"])
def checkin():
    """Daily check-in endpoint"""
    if "user_id" not in session:
        return jsonify({"error": "User not logged in"}), 401
    
    user_id = session.get("user_id")
    today = date.today()
    
    try:
        conn = get_db_connection()
        
        # Get current streak data
        streak_row = conn.execute(
            'SELECT * FROM user_streaks WHERE user_id = ?', (user_id,)
        ).fetchone()
        
        if not streak_row:
            # Initialize streak record
            conn.execute(
                'INSERT INTO user_streaks (user_id, streak_history) VALUES (?, ?)',
                (user_id, '{}')
            )
            conn.commit()
            streak_row = conn.execute(
                'SELECT * FROM user_streaks WHERE user_id = ?', (user_id,)
            ).fetchone()
        
        # Check if already checked in today
        if streak_row['last_checkin']:
            last_checkin = datetime.strptime(streak_row['last_checkin'], '%Y-%m-%d').date()
            if last_checkin == today:
                conn.close()
                return jsonify({
                    "error": "Already checked in today",
                    "current_streak": streak_row['current_streak'],
                    "message": "You've already checked in today!"
                }), 400
        
        # Calculate new streak
        current_streak = streak_row['current_streak']
        longest_streak = streak_row['longest_streak']
        total_checkins = streak_row['total_checkins']
        
        if streak_row['last_checkin']:
            last_checkin = datetime.strptime(streak_row['last_checkin'], '%Y-%m-%d').date()
            days_diff = (today - last_checkin).days
            
            if days_diff == 1:
                # Consecutive day
                current_streak += 1
            elif days_diff > 1:
                # Streak broken
                current_streak = 1
        else:
            # First checkin
            current_streak = 1
        
        # Update longest streak if needed
        if current_streak > longest_streak:
            longest_streak = current_streak
        
        total_checkins += 1
        
        # Update streak history
        try:
            streak_history = json.loads(streak_row['streak_history'] or '{}')
        except:
            streak_history = {}
        
        streak_history[today.isoformat()] = True
        
        # Update database
        conn.execute('''
            UPDATE user_streaks 
            SET current_streak = ?, longest_streak = ?, last_checkin = ?, 
                total_checkins = ?, streak_history = ?
            WHERE user_id = ?
        ''', (current_streak, longest_streak, today.isoformat(), 
              total_checkins, json.dumps(streak_history), user_id))
        
        conn.commit()
        conn.close()
        
        # Generate encouraging message
        messages = [
            f"Great job! You've maintained your streak for {current_streak} days!",
            f"Consistency is key! {current_streak} days and counting!",
            f"You're doing amazing! Day {current_streak} of your journey!",
            f"Keep it up! {current_streak} days of self-care!"
        ]
        
        message = random.choice(messages)
        
        return jsonify({
            "message": message,
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "total_checkins": total_checkins,
            "success": True
        })
        
    except Exception as e:
        app.logger.error(f"Error in checkin: {e}")
        return jsonify({"error": "Check-in failed. Please try again."}), 500

@app.route("/chat_history")
def chat_history():
    """Chat history page"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user_id = session.get("user_id")
    chat_history = get_user_chat_history(user_id) if user_id else []
    
    return render_template('chat_history.html', chat_history=chat_history)

@app.route("/get_chat_history", methods=["GET"])
def get_chat_history():
    """API endpoint to get chat history"""
    if "user_id" not in session:
        return jsonify({"error": "User not logged in"}), 401
    
    user_id = session.get("user_id")
    chat_history = get_user_chat_history(user_id)
    
    # Convert to list of dictionaries for JSON response
    history_list = []
    for chat in chat_history:
        history_list.append({
            "message": chat['message'],
            "response": chat['response'],
            "timestamp": chat['timestamp']
        })
    
    return jsonify({"chat_history": history_list})

@app.route("/sentiment_insights")
def sentiment_insights():
    """Sentiment insights and analytics page"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user_id = session.get("user_id")
    
    # Get user's sentiment data for analytics
    try:
        conn = get_db_connection()
        sentiment_rows = conn.execute("""
            SELECT sentiment, confidence_score, emotions_detected, crisis_flag, 
                   user_message, timestamp
            FROM sentiment_tracking 
            WHERE user_id = ? 
            ORDER BY timestamp DESC 
            LIMIT 50
        """, (user_id,)).fetchall()
        
        # Convert Row objects to dictionaries
        sentiment_data = []
        for row in sentiment_rows:
            sentiment_data.append({
                'sentiment': row['sentiment'],
                'confidence_score': row['confidence_score'],
                'emotions_detected': row['emotions_detected'],
                'crisis_flag': row['crisis_flag'],
                'user_message': row['user_message'],
                'timestamp': row['timestamp']
            })
        
        # Get chat history and convert to dictionaries
        chat_rows = conn.execute(
            'SELECT message, response, timestamp FROM chat_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?',
            (user_id, 50)
        ).fetchall()
        
        chat_history = []
        for row in chat_rows:
            chat_history.append({
                'message': row['message'],
                'response': row['response'],
                'timestamp': row['timestamp']
            })
        
        chat_history = list(reversed(chat_history))
        conn.close()
        
        return render_template('sentiment_insights.html', 
                             sentiment_data=sentiment_data, 
                             chat_history=chat_history)
    except Exception as e:
        app.logger.error(f"Error loading sentiment insights: {e}")
        return render_template('sentiment_insights.html', 
                             sentiment_data=[], 
                             chat_history=[])

@app.route("/static/<path:filename>")
def static_files(filename):
    """Serve static files"""
    return app.send_static_file(filename)

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
