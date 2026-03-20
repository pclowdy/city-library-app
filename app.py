from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import mysql.connector
import os
app = Flask(__name__)
# Secret key is required for session and flash messages!
app.secret_key = "city_library_super_secret_key" 
@app.route('/test')
def test():
    try:
        db = get_db_connection()
        db.close()
        return "Database connected successfully!"
    except Exception as e:
        return f"Database error: {str(e)}"
        
def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        database=os.environ.get("DB_NAME"),
        port=int(os.environ.get("DB_PORT", 3306)),
        connect_timeout=10
    )

# --- 1. MAIN DASHBOARD ---
@app.route('/')
def home():
    db = get_db_connection()
    cursor = db.cursor(dictionary=True) 
    
    category_filter = request.args.get('category')
    sort_order = request.args.get('sort', 'ASC')
    search_query = request.args.get('search', '').strip() # <--- NEW: Grab the search word!
    
    # Base query (Using 1=1 makes it easy to add AND conditions later)
    query = "SELECT * FROM AvailableBooks WHERE 1=1"
    params = []
    
    # NEW: If they typed a search, filter by title OR author
    if search_query:
        query += " AND (title LIKE %s OR author_name LIKE %s)"
        params.extend([f"%{search_query}%", f"%{search_query}%"])
    
    if category_filter and category_filter != 'All':
        query += " AND category_name = %s"
        params.append(category_filter)
        
    if sort_order == 'DESC':
        query += " ORDER BY title DESC"
    else:
        query += " ORDER BY title ASC"
        
    query += " LIMIT 50;"
    cursor.execute(query, tuple(params)) 
    books = cursor.fetchall()
    
    cursor.execute("SELECT DISTINCT category_name FROM Categories")
    categories = cursor.fetchall()

    user_favorites = []
    if 'member_id' in session:
        cursor.execute("SELECT book_id FROM Favorites WHERE member_id = %s", (session['member_id'],))
        favs = cursor.fetchall()
        user_favorites = [f['book_id'] for f in favs]
    
    for book in books:
        cursor.execute("SELECT year_of_publication, publisher FROM Books WHERE book_id = %s", (book['book_id'],))
        extra_details = cursor.fetchone()
        if extra_details:
            book['year'] = extra_details['year_of_publication']
            book['publisher'] = extra_details['publisher']
    
    cursor.close()
    db.close()
    
    return render_template('index.html', books=books, categories=categories, user_favorites=user_favorites)
# --- 2. AUTHENTICATION ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form['name']
        db = get_db_connection()
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM Members WHERE name = %s", (name,))
        member = cursor.fetchone()
        
        if not member:
            cursor.execute("INSERT INTO Members (name, join_date) VALUES (%s, CURDATE())", (name,))
            db.commit()
            session['member_id'] = cursor.lastrowid
            session['member_name'] = name
        else:
            session['member_id'] = member['member_id']
            session['member_name'] = member['name']
            
        cursor.close()
        db.close()
        return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form['password'] == os.environ.get("ADMIN_PASSWORD", "1234"):
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        return "Incorrect Password. <a href='/admin_login'>Try again</a>"
    return render_template('admin_login.html')

@app.route('/logout')
def logout():
    session.clear()
    return render_template('logout.html')


# --- 3. BOOK ISSUING, RETURNING & FAVORITES ---
@app.route('/issue/<book_id>', methods=['POST'])
def issue_book(book_id):
    if 'member_id' not in session:
        return redirect(url_for('login'))
        
    db = get_db_connection()
    cursor = db.cursor()
    
    # Check if the book is ALREADY issued to prevent duplicates
    cursor.execute("SELECT * FROM Issue WHERE book_id = %s", (book_id,))
    if cursor.fetchone():
        flash("This book is already issued !")
        cursor.close()
        db.close()
        return redirect(url_for('home'))
        
    # If not issued, issue it to the member
    try:
        cursor.execute("INSERT INTO Issue (book_id, member_id, issue_date) VALUES (%s, %s, CURDATE())", (book_id, session['member_id']))
        db.commit()
        flash("Book issued successfully!")
    except Exception as e:
        flash(f"Database error: {e}")
    finally:
        cursor.close()
        db.close()
        
    return redirect(url_for('home'))

@app.route('/return_book/<book_id>', methods=['GET'])
def return_book(book_id):
    if 'member_id' not in session:
        return redirect(url_for('login'))
        
    db = get_db_connection()
    cursor = db.cursor()
    
    try:
        # Delete the issue record for this user and book
        cursor.execute("DELETE FROM Issue WHERE book_id = %s AND member_id = %s", (book_id, session['member_id']))
        db.commit()
        flash("Book returned successfully!")
    except Exception as e:
        flash(f"Database error: {e}")
    finally:
        cursor.close()
        db.close()
        
    return redirect(url_for('issued'))

# Background route for the Heart Icon toggle
@app.route('/toggle_favorite', methods=['POST'])
def toggle_favorite():
    if 'member_id' not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401
        
    data = request.get_json()
    book_id = data['book_id']
    member_id = session['member_id']
    
    db = get_db_connection()
    cursor = db.cursor()
    
    # Check if already favorited
    cursor.execute("SELECT * FROM Favorites WHERE member_id = %s AND book_id = %s", (member_id, book_id))
    if cursor.fetchone():
        cursor.execute("DELETE FROM Favorites WHERE member_id = %s AND book_id = %s", (member_id, book_id))
        action = "removed"
    else:
        cursor.execute("INSERT INTO Favorites (member_id, book_id) VALUES (%s, %s)", (member_id, book_id))
        action = "added"
        
    db.commit()
    cursor.close()
    db.close()
    return jsonify({"status": "success", "action": action})


# --- 4. DASHBOARD PAGES & ADMIN LOGIC ---
@app.route('/favorites')
def favorites():
    if 'member_id' not in session: return redirect(url_for('login'))
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    
    cursor.execute("""
        SELECT b.*, a.name AS author_name 
        FROM Favorites f 
        JOIN Books b ON f.book_id = b.book_id 
        JOIN Authors a ON b.author_id = a.author_id 
        WHERE f.member_id = %s
    """, (session['member_id'],))
    books = cursor.fetchall()
    cursor.close()
    db.close()
    return render_template('favorites.html', books=books)

@app.route('/issued')
def issued():
    if 'member_id' not in session: return redirect(url_for('login'))
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    
    # FIXED: Added b.book_id so the return button works
    cursor.execute("""
        SELECT b.book_id, b.title, b.image_url, a.name AS author_name, i.issue_date 
        FROM Issue i 
        JOIN Books b ON i.book_id = b.book_id 
        JOIN Authors a ON b.author_id = a.author_id 
        WHERE i.member_id = %s
    """, (session['member_id'],))
    books = cursor.fetchall()
    cursor.close()
    db.close()
    return render_template('issued.html', books=books)

@app.route('/admin_dashboard')
def admin_dashboard():
    if 'admin_logged_in' not in session: return redirect(url_for('admin_login'))
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    
    # 1. Get Members and their currently issued books
    cursor.execute("""
        SELECT m.name AS member_name, b.title AS book_title, i.issue_date
        FROM Members m
        LEFT JOIN Issue i ON m.member_id = i.member_id
        LEFT JOIN Books b ON i.book_id = b.book_id
    """)
    member_data = cursor.fetchall()
    
    # 2. Get all books for the delete dropdown
    cursor.execute("SELECT book_id, title FROM Books ORDER BY title ASC")
    all_books = cursor.fetchall()
    
    cursor.close()
    db.close()
    return render_template('admin_dashboard.html', member_data=member_data, all_books=all_books)

# --- ADMIN ACTIONS ---
@app.route('/admin/add_book', methods=['POST'])
def add_book():
    if 'admin_logged_in' not in session: return redirect(url_for('admin_login'))
    db = get_db_connection()
    cursor = db.cursor()
    try:
        cursor.execute("""
            INSERT INTO Books (book_id, title, author_id, category_id, year_of_publication, publisher, copies) 
            VALUES (%s, %s, 1, 1, %s, %s, %s)
        """, (request.form['isbn'], request.form['title'], request.form['year'], request.form['publisher'], request.form['copies']))
        db.commit()
    except Exception as e:
        return f"Error adding book: {e}"
    finally:
        cursor.close()
        db.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_book', methods=['POST'])
def delete_book():
    if 'admin_logged_in' not in session: return redirect(url_for('admin_login'))
    book_id = request.form['book_id']
    db = get_db_connection()
    cursor = db.cursor()
    
    # Check: Ensure book is not currently issued before deleting
    cursor.execute("SELECT * FROM Issue WHERE book_id = %s", (book_id,))
    if cursor.fetchone():
        return "<h3>Cannot delete!</h3><p>Someone is currently borrowing this book. All copies must be returned first.</p><a href='/admin_dashboard'>Go back</a>"
        
    try:
        # Delete from favorites first, then delete the book
        cursor.execute("DELETE FROM Favorites WHERE book_id = %s", (book_id,))
        cursor.execute("DELETE FROM Books WHERE book_id = %s", (book_id,))
        db.commit()
    except Exception as e:
        return f"Error deleting book: {e}"
    finally:
        cursor.close()
        db.close()
    return redirect(url_for('admin_dashboard'))

if __name__ == '__main__':
    app.run(debug=True)
