from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, jsonify
from aip import AipFace
import base64
import sqlite3
import hashlib
import os
import json
from datetime import datetime
from werkzeug.utils import secure_filename
import urllib.parse

app = Flask(__name__)
app.secret_key = 'your-secret-key-here-change-in-production'

# ==================== 百度AI人脸识别配置 ====================
APP_ID = '123044418'
API_KEY = 'VWtQmoKO44mDYdD3vGlXbi0I'
SECRET_KEY = 'BsnaarSH1Bv5TfC8nWDTLYtKTln70whR'

face_client = AipFace(APP_ID, API_KEY, SECRET_KEY)
GROUP_ID = 'equipment_users'

# 文件上传配置
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

DATABASE = 'equipment_system.db'

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            equipment_name TEXT NOT NULL,
            quantity TEXT,
            photos TEXT,
            purpose TEXT NOT NULL,
            expected_date DATE,
            status TEXT DEFAULT 'pending',
            reject_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    cursor.execute("SELECT * FROM users WHERE role = 'admin'")
    if not cursor.fetchone():
        admin_password = hashlib.sha256('admin123'.encode()).hexdigest()
        cursor.execute(
            'INSERT INTO users (username, password, role) VALUES (?, ?, ?)',
            ('admin', admin_password, 'admin')
        )
        print("✅ 管理员账号已创建: admin / admin123")
    
    conn.commit()
    conn.close()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_uploaded_files(files):
    saved_files = []
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
            import uuid
            unique_id = str(uuid.uuid4())[:8]
            unique_filename = f"{timestamp}{unique_id}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            file.save(filepath)
            saved_files.append(unique_filename)
        else:
            saved_files.append('')
    return saved_files

@app.route('/')
def index():
    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin_panel'))
        else:
            return redirect(url_for('user_panel'))
    return redirect(url_for('login_page'))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'GET':
        return render_template('login.html')
    
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    
    if not username or not password:
        return render_template('login.html', error='用户名和密码不能为空', username=username)
    
    conn = get_db()
    user = conn.execute(
        'SELECT * FROM users WHERE username = ? AND password = ?',
        (username, hash_password(password))
    ).fetchone()
    conn.close()
    
    if user:
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['role'] = user['role']
        
        if user['role'] == 'admin':
            return redirect(url_for('admin_panel'))
        else:
            return redirect(url_for('user_panel'))
    else:
        return render_template('login.html', error='用户名或密码错误', username=username)

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'GET':
        return render_template('register.html')
    
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    confirm_password = request.form.get('confirm_password', '').strip()
    face_image = request.form.get('face_descriptor', '')  # 获取人脸照片
    
    if not username or not password:
        return render_template('register.html', error='用户名和密码不能为空', username=username)
    
    if password != confirm_password:
        return render_template('register.html', error='两次输入的密码不一致', username=username)
    
    conn = get_db()
    
    existing_user = conn.execute(
        'SELECT id FROM users WHERE username = ?', (username,)
    ).fetchone()
    
    if existing_user:
        conn.close()
        return render_template('register.html', error='用户名已存在', username=username)
    
    try:
        # 创建用户
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO users (username, password, role) VALUES (?, ?, ?)',
            (username, hash_password(password), 'user')
        )
        user_id = cursor.lastrowid
        conn.commit()
        
        # 如果有人脸照片，注册到百度AI
        if face_image:
            if 'base64,' in face_image:
                face_image = face_image.split('base64,')[1]
            
            result = face_client.addUser(
                image=face_image,
                image_type='BASE64',
                group_id=GROUP_ID,
                user_id=str(user_id),
            )
            print(f"人脸注册结果: {result}")
            
            if result.get('error_code') != 0:
                # 人脸注册失败，但用户已创建，记录错误
                print(f"⚠️ 用户 {username} 创建成功，但人脸注册失败: {result.get('error_msg')}")
        
        conn.close()
        return render_template('login.html', success='注册成功！请登录')
        
    except Exception as e:
        conn.close()
        return render_template('register.html', error=f'注册失败: {str(e)}', username=username)
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/user')
def user_panel():
    if 'user_id' not in session or session.get('role') != 'user':
        return redirect(url_for('login_page'))
    
    conn = get_db()
    applications = conn.execute('''
        SELECT * FROM applications 
        WHERE user_id = ? 
        ORDER BY created_at DESC
    ''', (session['user_id'],)).fetchall()
    
    pending_count = sum(1 for app in applications if app['status'] == 'pending')
    approved_count = sum(1 for app in applications if app['status'] == 'approved')
    rejected_count = sum(1 for app in applications if app['status'] == 'rejected')
    
    conn.close()
    
    applications_list = [dict(app) for app in applications]
    
    return render_template('user.html',
                         applications=applications,
                         applications_json=json.dumps(applications_list),
                         pending_count=pending_count,
                         approved_count=approved_count,
                         rejected_count=rejected_count,
                         total_count=len(applications))

@app.route('/admin')
def admin_panel():
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))
    
    conn = get_db()
    
    applications = conn.execute('''
        SELECT a.*, u.username 
        FROM applications a 
        JOIN users u ON a.user_id = u.id 
        ORDER BY 
            CASE a.status 
                WHEN 'pending' THEN 1 
                WHEN 'approved' THEN 2 
                ELSE 3 
            END,
            a.created_at DESC
    ''').fetchall()
    
    pending_count = sum(1 for app in applications if app['status'] == 'pending')
    approved_count = sum(1 for app in applications if app['status'] == 'approved')
    rejected_count = sum(1 for app in applications if app['status'] == 'rejected')
    user_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'user'").fetchone()[0]
    
    users = conn.execute('''
        SELECT 
            u.id,
            u.username,
            u.role,
            u.created_at,
            COUNT(a.id) as application_count
        FROM users u
        LEFT JOIN applications a ON u.id = a.user_id
        GROUP BY u.id
        ORDER BY u.created_at DESC
    ''').fetchall()
    
    conn.close()
    
    applications_list = [dict(app) for app in applications]
    
    return render_template('admin.html',
                         applications=applications,
                         applications_json=json.dumps(applications_list),
                         pending_count=pending_count,
                         approved_count=approved_count,
                         rejected_count=rejected_count,
                         user_count=user_count,
                         users=users)

@app.route('/apply', methods=['POST'])
def apply_equipment():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    
    equipment_names = request.form.getlist('equipment_names[]')
    quantities = request.form.getlist('quantities[]')
    
    valid_equipment = []
    valid_quantities = []
    for i, name in enumerate(equipment_names):
        if name and name.strip():
            valid_equipment.append(name.strip())
            qty = quantities[i] if i < len(quantities) else '1'
            valid_quantities.append(qty if qty else '1')
    
    if not valid_equipment:
        return redirect(url_for('user_panel'))
    
    equipment_name_str = '|||'.join(valid_equipment)
    quantity_str = '|||'.join(valid_quantities)
    
    photos = request.files.getlist('equipment_photos[]')
    saved_photos = save_uploaded_files(photos)
    photos_str = '|||'.join(saved_photos)
    
    purpose = request.form.get('purpose', '').strip()
    expected_date = request.form.get('expected_date', '')
    
    if not purpose:
        return redirect(url_for('user_panel'))
    
    conn = get_db()
    conn.execute('''
        INSERT INTO applications 
        (user_id, equipment_name, quantity, photos, purpose, expected_date) 
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (session['user_id'], equipment_name_str, quantity_str, photos_str, purpose, expected_date))
    conn.commit()
    conn.close()
    
    return redirect(url_for('user_panel'))

@app.route('/approve/<int:app_id>', methods=['POST'])
def approve_application(app_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))
    
    conn = get_db()
    conn.execute("UPDATE applications SET status = 'approved' WHERE id = ?", (app_id,))
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_panel'))

@app.route('/reject/<int:app_id>', methods=['POST'])
def reject_application(app_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))
    
    reject_reason = request.form.get('reject_reason', '').strip()
    if not reject_reason:
        reject_reason = '未提供拒绝原因'
    
    conn = get_db()
    conn.execute(
        "UPDATE applications SET status = 'rejected', reject_reason = ? WHERE id = ?",
        (reject_reason, app_id)
    )
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_panel'))

@app.route('/cancel/<int:app_id>', methods=['POST'])
def cancel_application(app_id):
    if 'user_id' not in session:
        return redirect(url_for('login_page'))
    
    conn = get_db()
    app_info = conn.execute(
        "SELECT photos FROM applications WHERE id = ? AND user_id = ? AND status = 'pending'",
        (app_id, session['user_id'])
    ).fetchone()
    
    if app_info and app_info['photos']:
        photos = app_info['photos'].split('|||')
        for photo in photos:
            if photo:
                photo_path = os.path.join(app.config['UPLOAD_FOLDER'], photo)
                if os.path.exists(photo_path):
                    try:
                        os.remove(photo_path)
                    except:
                        pass
    
    conn.execute(
        "DELETE FROM applications WHERE id = ? AND user_id = ? AND status = 'pending'",
        (app_id, session['user_id'])
    )
    conn.commit()
    conn.close()
    
    return redirect(url_for('user_panel'))

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ==================== 接收ESP32指令 ====================
@app.route('/command', methods=['GET'])
def receive_command():
    """接收ESP32发来的器材名称并自动创建申请"""
    try:
        client_ip = request.remote_addr
        receive_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        equipment_name = request.args.get('cmd', '')
        equipment_name = urllib.parse.unquote(equipment_name)
        
        if not equipment_name:
            return "ERROR: Empty command", 400
        
        print("\n" + "="*60)
        print(f"📨 收到来自 {client_ip} 的申请")
        print(f"⏰ 接收时间: {receive_time}")
        print(f"📝 器材名称: {equipment_name}")
        print("="*60 + "\n")
        
        conn = get_db()
        
        esp32_user = conn.execute(
            "SELECT id FROM users WHERE username = 'esp32_device'"
        ).fetchone()
        
        if not esp32_user:
            device_password = hashlib.sha256('esp32_device_auto'.encode()).hexdigest()
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO users (username, password, role) VALUES (?, ?, ?)',
                ('esp32_device', device_password, 'user')
            )
            user_id = cursor.lastrowid
            conn.commit()
        else:
            user_id = esp32_user['id']
        
        purpose = f"[自动申请] 来自ESP32设备 ({client_ip})"
        
        conn.execute('''
            INSERT INTO applications 
            (user_id, equipment_name, quantity, purpose, status) 
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, equipment_name, '1', purpose, 'pending'))
        
        conn.commit()
        app_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        
        print(f"✅ 已自动创建器材申请 (ID: #{app_id})")
        print("="*60 + "\n")
        return f"OK", 200
        
    except Exception as e:
        print(f"❌ 处理指令错误: {e}")
        return f"ERROR: {str(e)}", 500

# ==================== 人脸识别API路由 ====================
@app.route('/api/face_register', methods=['POST'])
def api_face_register():
    """人脸注册"""
    try:
        data = request.get_json()
        user_id = str(session.get('user_id', ''))
        image_base64 = data.get('image', '')
        
        if not user_id:
            return jsonify({'success': False, 'message': '请先登录'})
        if not image_base64:
            return jsonify({'success': False, 'message': '请提供人脸照片'})
        
        if 'base64,' in image_base64:
            image_base64 = image_base64.split('base64,')[1]
        
        result = face_client.addUser(
            image=image_base64,
            image_type='BASE64',
            group_id=GROUP_ID,
            user_id=user_id,
            user_info=session.get('username', '')
        )
        
        print(f"人脸注册结果: {result}")
        
        if result.get('error_code') == 0:
            return jsonify({'success': True, 'message': '人脸注册成功'})
        else:
            return jsonify({'success': False, 'message': result.get('error_msg', '注册失败')})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/face_login', methods=['POST'])
def api_face_login():
    """人脸登录"""
    try:
        data = request.get_json()
        image_base64 = data.get('image', '')
        
        if not image_base64:
            return jsonify({'success': False, 'message': '请提供人脸照片'})
        
        if 'base64,' in image_base64:
            image_base64 = image_base64.split('base64,')[1]
        
        result = face_client.search(
            image=image_base64,
            image_type='BASE64',
            group_id_list=GROUP_ID,
        )
        
        print(f"人脸搜索结果: {result}")
        
        if result.get('error_code') == 0:
            user_list = result.get('result', {}).get('user_list', [])
            if user_list and len(user_list) > 0:
                top_user = user_list[0]
                score = float(top_user.get('score', 0))  # 强制转换为浮点数
                user_id = top_user.get('user_id', '')
                
                if score > 70:
                    conn = get_db()
                    user = conn.execute('SELECT * FROM users WHERE id = ?', (int(user_id),)).fetchone()
                    conn.close()
                    
                    if user:
                        session['user_id'] = user['id']
                        session['username'] = user['username']
                        session['role'] = user['role']
                        
                        redirect_url = '/admin' if user['role'] == 'admin' else '/user'
                        return jsonify({
                            'success': True,
                            'message': f'欢迎 {user["username"]}',
                            'redirect': redirect_url
                        })
                
                return jsonify({'success': False, 'message': f'置信度过低({score:.1f})'})
            return jsonify({'success': False, 'message': '未找到匹配用户'})
        else:
            return jsonify({'success': False, 'message': result.get('error_msg', '识别失败')})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    
@app.route('/make_admin/<int:user_id>', methods=['POST'])
def make_admin(user_id):
    """将普通用户设为管理员"""
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))
    conn = get_db()
    conn.execute(
           "UPDATE users SET role = 'admin' WHERE id = ? AND role = 'user'",
            (user_id,)
        )
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/remove_admin/<int:user_id>', methods=['POST'])
def remove_admin(user_id):
    """移除管理员权限（降为普通用户）"""
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))
    
    # 不能移除自己的管理员权限
    if user_id == session['user_id']:
        return redirect(url_for('admin_panel'))
    
    conn = get_db()
    
    # 检查是否还有其他管理员
    admin_count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin'"
    ).fetchone()[0]
    
    if admin_count <= 1:
        conn.close()
        return redirect(url_for('admin_panel'))
    
    conn.execute(
        "UPDATE users SET role = 'user' WHERE id = ? AND role = 'admin'",
        (user_id,)
    )
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_panel'))
# ==================== 接收ESP32人脸识别结果 ====================

@app.route('/api/face_notification', methods=['POST'])
def api_face_notification():
    """接收ESP32发来的人脸识别通知，并自动创建申请"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'message': '无效数据'}), 400
        
        event = data.get('event', '')
        user_id = data.get('user_id', '')
        score = data.get('score', 0)
        timestamp = data.get('timestamp', '')
        message = data.get('message', '')
        device = data.get('device', 'esp32')
        
        # 打印接收到的数据
        print("\n" + "="*60)
        print(f"📨 收到来自 {device} 的人脸识别通知")
        print(f"⏰ 时间: {timestamp}")
        print(f"👤 用户ID: {user_id}")
        print(f"🎯 匹配分数: {score}")
        print(f"📝 消息: {message}")
        print("="*60 + "\n")
        
        # 查找用户
        conn = get_db()
        user = conn.execute(
            'SELECT * FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        
        if not user:
            conn.close()
            print(f"⚠️ 用户 {user_id} 不存在")
            return jsonify({'success': False, 'message': '用户不存在'}), 404
        
        print(f"✅ 识别用户: {user['username']} (角色: {user['role']})")
        
        # 获取该用户最近的申请记录
        recent_apps = conn.execute('''
            SELECT * FROM applications 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 5
        ''', (user_id,)).fetchall()
        
        conn.close()
        
        return jsonify({
            'success': True,
            'message': '通知已接收',
            'user': {
                'id': user['id'],
                'username': user['username'],
                'role': user['role']
            },
            'recent_applications': [dict(app) for app in recent_apps]
        })
        
    except Exception as e:
        print(f"❌ 处理通知错误: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/face_apply', methods=['POST'])
def api_face_apply():
    """接收ESP32人脸识别结果，并自动为该用户创建器材申请"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'message': '无效数据'}), 400
        
        user_id = data.get('user_id', '')
        equipment_name = data.get('equipment_name', '')
        quantity = data.get('quantity', '1')
        score = data.get('score', 0)
        device = data.get('device', 'esp32')
        timestamp = data.get('timestamp', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 打印接收到的数据
        print("\n" + "="*60)
        print(f"📨 收到来自 {device} 的人脸识别申请")
        print(f"⏰ 时间: {timestamp}")
        print(f"👤 用户ID: {user_id}")
        print(f"🎯 匹配分数: {score}")
        print(f"📝 申请器材: {equipment_name} x {quantity}")
        print("="*60 + "\n")
        
        if not equipment_name:
            return jsonify({'success': False, 'message': '缺少器材名称'}), 400
        
        conn = get_db()
        
        # 查找用户
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        
        if not user:
            conn.close()
            return jsonify({'success': False, 'message': '用户不存在'}), 404
        
        # 创建器材申请
        purpose = f"[人脸识别自动申请] 来自{device}设备，匹配分数: {score}"
        
        conn.execute('''
            INSERT INTO applications 
            (user_id, equipment_name, quantity, purpose, status) 
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, equipment_name, str(quantity), purpose, 'pending'))
        
        conn.commit()
        app_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        
        print(f"✅ 已为 {user['username']} 自动创建器材申请 (ID: #{app_id})")
        print(f"   器材: {equipment_name} x {quantity}")
        print("="*60 + "\n")
        
        return jsonify({
            'success': True,
            'message': f'已为 {user["username"]} 创建申请',
            'application_id': app_id,
            'equipment': equipment_name,
            'quantity': quantity
        })
        
    except Exception as e:
        print(f"❌ 处理申请错误: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
if __name__ == '__main__':
    if not os.path.exists('templates'):
        os.makedirs('templates')
    if not os.path.exists('static'):
        os.makedirs('static')
    
    init_db()
    
    print("\n" + "="*50)
    print("🚀 器材申请管理系统启动成功！")
    print("📍 访问地址: http://127.0.0.1:5000")
    print("👤 管理员账号: admin / admin123")
    print("📷 人脸识别: 百度AI API")
    print("📁 上传文件目录: static/uploads/")
    print("="*50 + "\n")
    app.run(debug=True, host='0.0.0.0', port=5000, ssl_context=('cert.pem', 'key.pem'))