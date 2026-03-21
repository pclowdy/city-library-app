from contextlib import closing
import os
import time

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
import mysql.connector
from mysql.connector import pooling

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "city_library_super_secret_key")

DB_TABLES = {
    "books": os.environ.get("TABLE_BOOKS", "Books"),
    "authors": os.environ.get("TABLE_AUTHORS", "Authors"),
    "categories": os.environ.get("TABLE_CATEGORIES", "Categories"),
    "members": os.environ.get("TABLE_MEMBERS", "Members"),
    "favorites": os.environ.get("TABLE_FAVORITES", "Favorites"),
    "issue": os.environ.get("TABLE_ISSUE", "Issue"),
    "available_books": os.environ.get("VIEW_AVAILABLE_BOOKS", "AvailableBooks"),
}

POOL = pooling.MySQLConnectionPool(
    pool_name="bookbase_pool",
    pool_size=int(os.environ.get("DB_POOL_SIZE", 5)),
    pool_reset_session=True,
    host=os.environ.get("DB_HOST"),
    user=os.environ.get("DB_USER"),
    password=os.environ.get("DB_PASSWORD"),
    database=os.environ.get("DB_NAME"),
    port=int(os.environ.get("DB_PORT", 3306)),
    connect_timeout=10,
    autocommit=False,
)

_CATEGORY_CACHE = {"data": [], "expires_at": 0}
_CATEGORY_CACHE_TTL = 300
HOME_PAGE_LIMIT = int(os.environ.get("HOME_PAGE_LIMIT", 24))


def get_db_connection():
    return POOL.get_connection()


def get_categories():
    now = time.time()
    if _CATEGORY_CACHE["data"] and _CATEGORY_CACHE["expires_at"] > now:
        return _CATEGORY_CACHE["data"]

    query = f"SELECT DISTINCT category_name FROM {DB_TABLES['categories']} ORDER BY category_name ASC"
    with closing(get_db_connection()) as db, closing(db.cursor(dictionary=True)) as cursor:
        cursor.execute(query)
        categories = cursor.fetchall()

    _CATEGORY_CACHE["data"] = categories
    _CATEGORY_CACHE["expires_at"] = now + _CATEGORY_CACHE_TTL
    return categories


@app.route('/test')
def test():
    try:
        with closing(get_db_connection()) as db:
            with closing(db.cursor()) as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
        return 'Database connected successfully!'
    except Exception as e:
        return f'Database error: {str(e)}', 500


@app.route('/')
def home():
    category_filter = request.args.get('category', '').strip()
    sort_order = 'DESC' if request.args.get('sort', 'ASC').upper() == 'DESC' else 'ASC'
    search_query = request.args.get('search', '').strip()

    base_query = f"""
        SELECT
            ab.book_id,
            ab.title,
            ab.author_name,
            ab.category_name,
            ab.image_url,
            ab.copies,
            b.year_of_publication AS year,
            b.publisher
        FROM {DB_TABLES['available_books']} ab
        JOIN {DB_TABLES['books']} b ON b.book_id = ab.book_id
        WHERE 1=1
    """
    params = []

    if search_query:
        like_value = f"%{search_query}%"
        base_query += " AND (ab.title LIKE %s OR ab.author_name LIKE %s)"
        params.extend([like_value, like_value])

    if category_filter and category_filter != 'All':
        base_query += ' AND ab.category_name = %s'
        params.append(category_filter)

    base_query += f' ORDER BY ab.title {sort_order} LIMIT %s'
    params.append(HOME_PAGE_LIMIT)

    with closing(get_db_connection()) as db, closing(db.cursor(dictionary=True)) as cursor:
        cursor.execute(base_query, tuple(params))
        books = cursor.fetchall()

        user_favorites = []
        if 'member_id' in session:
            cursor.execute(
                f"SELECT book_id FROM {DB_TABLES['favorites']} WHERE member_id = %s",
                (session['member_id'],),
            )
            user_favorites = [row['book_id'] for row in cursor.fetchall()]

    categories = get_categories()
    return render_template(
        'index.html',
        books=books,
        categories=categories,
        user_favorites=user_favorites,
    )


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form['name'].strip()
        if not name:
            flash('Please enter your name.')
            return render_template('login.html')

        with closing(get_db_connection()) as db, closing(db.cursor(dictionary=True)) as cursor:
            cursor.execute(f"SELECT member_id, name FROM {DB_TABLES['members']} WHERE name = %s", (name,))
            member = cursor.fetchone()

            if not member:
                cursor.execute(
                    f"INSERT INTO {DB_TABLES['members']} (name, join_date) VALUES (%s, CURDATE())",
                    (name,),
                )
                db.commit()
                session['member_id'] = cursor.lastrowid
                session['member_name'] = name
            else:
                session['member_id'] = member['member_id']
                session['member_name'] = member['name']

        return redirect(url_for('home'))

    return render_template('login.html')


@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form['password'] == os.environ.get('ADMIN_PASSWORD', '1234'):
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        return "Incorrect Password. <a href='/admin_login'>Try again</a>"
    return render_template('admin_login.html')


@app.route('/logout')
def logout():
    session.clear()
    return render_template('logout.html')


@app.route('/issue/<book_id>', methods=['POST'])
def issue_book(book_id):
    if 'member_id' not in session:
        return redirect(url_for('login'))

    with closing(get_db_connection()) as db, closing(db.cursor(dictionary=True)) as cursor:
        try:
            cursor.execute(
                f"SELECT issue_id FROM {DB_TABLES['issue']} WHERE book_id = %s LIMIT 1",
                (book_id,),
            )
            if cursor.fetchone():
                flash('This book is already issued!')
                return redirect(url_for('home'))

            cursor.execute(
                f"SELECT copies FROM {DB_TABLES['books']} WHERE book_id = %s LIMIT 1",
                (book_id,),
            )
            book = cursor.fetchone()
            if not book:
                flash('Book not found.')
                return redirect(url_for('home'))
            if int(book['copies'] or 0) <= 0:
                flash('This book is currently unavailable.')
                return redirect(url_for('home'))

            cursor.execute(
                f"INSERT INTO {DB_TABLES['issue']} (book_id, member_id, issue_date) VALUES (%s, %s, CURDATE())",
                (book_id, session['member_id']),
            )
            db.commit()
            flash('Book issued successfully!')
        except Exception as e:
            db.rollback()
            flash(f'Database error: {e}')

    return redirect(url_for('home'))


@app.route('/return_book/<book_id>', methods=['GET'])
def return_book(book_id):
    if 'member_id' not in session:
        return redirect(url_for('login'))

    with closing(get_db_connection()) as db, closing(db.cursor()) as cursor:
        try:
            cursor.execute(
                f"DELETE FROM {DB_TABLES['issue']} WHERE book_id = %s AND member_id = %s",
                (book_id, session['member_id']),
            )
            db.commit()
            flash('Book returned successfully!')
        except Exception as e:
            db.rollback()
            flash(f'Database error: {e}')

    return redirect(url_for('issued'))


@app.route('/toggle_favorite', methods=['POST'])
def toggle_favorite():
    if 'member_id' not in session:
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401

    data = request.get_json(silent=True) or {}
    book_id = data.get('book_id')
    if not book_id:
        return jsonify({'status': 'error', 'message': 'Missing book id'}), 400

    member_id = session['member_id']
    with closing(get_db_connection()) as db, closing(db.cursor()) as cursor:
        cursor.execute(
            f"SELECT 1 FROM {DB_TABLES['favorites']} WHERE member_id = %s AND book_id = %s LIMIT 1",
            (member_id, book_id),
        )
        if cursor.fetchone():
            cursor.execute(
                f"DELETE FROM {DB_TABLES['favorites']} WHERE member_id = %s AND book_id = %s",
                (member_id, book_id),
            )
            action = 'removed'
        else:
            cursor.execute(
                f"INSERT INTO {DB_TABLES['favorites']} (member_id, book_id) VALUES (%s, %s)",
                (member_id, book_id),
            )
            action = 'added'
        db.commit()

    return jsonify({'status': 'success', 'action': action})


@app.route('/favorites')
def favorites():
    if 'member_id' not in session:
        return redirect(url_for('login'))

    query = f"""
        SELECT
            b.book_id,
            b.title,
            b.image_url,
            b.year_of_publication AS year,
            b.publisher,
            a.name AS author_name,
            c.category_name
        FROM {DB_TABLES['favorites']} f
        JOIN {DB_TABLES['books']} b ON f.book_id = b.book_id
        JOIN {DB_TABLES['authors']} a ON b.author_id = a.author_id
        JOIN {DB_TABLES['categories']} c ON b.category_id = c.category_id
        WHERE f.member_id = %s
        ORDER BY b.title ASC
    """

    with closing(get_db_connection()) as db, closing(db.cursor(dictionary=True)) as cursor:
        cursor.execute(query, (session['member_id'],))
        books = cursor.fetchall()

    return render_template('favorites.html', books=books)


@app.route('/issued')
def issued():
    if 'member_id' not in session:
        return redirect(url_for('login'))

    query = f"""
        SELECT
            b.book_id,
            b.title,
            b.image_url,
            a.name AS author_name,
            i.issue_date
        FROM {DB_TABLES['issue']} i
        JOIN {DB_TABLES['books']} b ON i.book_id = b.book_id
        JOIN {DB_TABLES['authors']} a ON b.author_id = a.author_id
        WHERE i.member_id = %s
        ORDER BY i.issue_date DESC, b.title ASC
    """

    with closing(get_db_connection()) as db, closing(db.cursor(dictionary=True)) as cursor:
        cursor.execute(query, (session['member_id'],))
        books = cursor.fetchall()

    return render_template('issued.html', books=books)


@app.route('/admin_dashboard')
def admin_dashboard():
    if 'admin_logged_in' not in session:
        return redirect(url_for('admin_login'))

    with closing(get_db_connection()) as db, closing(db.cursor(dictionary=True)) as cursor:
        cursor.execute(
            f"""
            SELECT m.name AS member_name, b.title AS book_title, i.issue_date
            FROM {DB_TABLES['members']} m
            LEFT JOIN {DB_TABLES['issue']} i ON m.member_id = i.member_id
            LEFT JOIN {DB_TABLES['books']} b ON i.book_id = b.book_id
            ORDER BY m.name ASC, i.issue_date DESC
            """
        )
        member_data = cursor.fetchall()

        cursor.execute(f"SELECT book_id, title FROM {DB_TABLES['books']} ORDER BY title ASC")
        all_books = cursor.fetchall()

    return render_template('admin_dashboard.html', member_data=member_data, all_books=all_books)


@app.route('/admin/add_book', methods=['POST'])
def add_book():
    if 'admin_logged_in' not in session:
        return redirect(url_for('admin_login'))

    with closing(get_db_connection()) as db, closing(db.cursor()) as cursor:
        try:
            cursor.execute(
                f"""
                INSERT INTO {DB_TABLES['books']}
                (book_id, title, author_id, category_id, year_of_publication, publisher, copies)
                VALUES (%s, %s, 1, 1, %s, %s, %s)
                """,
                (
                    request.form['isbn'],
                    request.form['title'],
                    request.form['year'],
                    request.form['publisher'],
                    request.form['copies'],
                ),
            )
            db.commit()
            _CATEGORY_CACHE['expires_at'] = 0
        except Exception as e:
            db.rollback()
            return f'Error adding book: {e}'

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete_book', methods=['POST'])
def delete_book():
    if 'admin_logged_in' not in session:
        return redirect(url_for('admin_login'))

    book_id = request.form['book_id']
    with closing(get_db_connection()) as db, closing(db.cursor()) as cursor:
        cursor.execute(f"SELECT 1 FROM {DB_TABLES['issue']} WHERE book_id = %s LIMIT 1", (book_id,))
        if cursor.fetchone():
            return "<h3>Cannot delete!</h3><p>Someone is currently borrowing this book. All copies must be returned first.</p><a href='/admin_dashboard'>Go back</a>"

        try:
            cursor.execute(f"DELETE FROM {DB_TABLES['favorites']} WHERE book_id = %s", (book_id,))
            cursor.execute(f"DELETE FROM {DB_TABLES['books']} WHERE book_id = %s", (book_id,))
            db.commit()
            _CATEGORY_CACHE['expires_at'] = 0
        except Exception as e:
            db.rollback()
            return f'Error deleting book: {e}'

    return redirect(url_for('admin_dashboard'))


if __name__ == '__main__':
    app.run(debug=True)
