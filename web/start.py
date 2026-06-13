# python3 -m venv venv 先开虚拟环境
# pip install flask flask-cors baidu-aip chardet requests urllib3 -i 
# https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
# 前置库安装
# source source venv/bin/activate
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

# ==================== 云台初始化（树莓派环境） ====================
import threading

_pan_tilt_ready = False
_picam2 = None
_detector = None
_camera_lock = threading.Lock()

try:
    from agent_yuntai.panTilt import lookAt
    from picamera2 import Picamera2
    import apriltag

    _picam2 = Picamera2()
    _picam2.configure(_picam2.create_preview_configuration(main={"size": (3280, 2464)}))
    _detector = apriltag.apriltag("tag36h11", threads=4)
    _pan_tilt_ready = True
    print("✅ 云台模块已就绪")
except Exception as e:
    _pan_tilt_ready = False
    print(f"⚠️ 云台模块不可用（非树莓派环境或缺少依赖）")


def _pan_to_cabinet_sync(cabinet_id):
    """同步执行云台指向（在独立线程中调用）"""
    global _picam2
    with _camera_lock:
        try:
            _picam2.start()
            lookAt(cabinet_id, _picam2, _detector)
        finally:
            _picam2.stop()


def pan_to_cabinet(cabinet_id):
    """异步将云台指向 cabinet_id 对应的 AprilTag（用完即释放摄像头）"""
    if _pan_tilt_ready:
        threading.Thread(
            target=_pan_to_cabinet_sync, args=(cabinet_id,),
            daemon=True
        ).start()
        print(f"📷 云台指向 tag_id={cabinet_id}")


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
            cabinet_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # 迁移：旧数据库没有 cabinet_id 列时自动新增
    try:
        cursor.execute("ALTER TABLE applications ADD COLUMN cabinet_id INTEGER")
    except:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cabinets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cabinet_number TEXT NOT NULL,
            equipment_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cabinet_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cabinet_id INTEGER NOT NULL,
            component_name TEXT NOT NULL,
            FOREIGN KEY (cabinet_id) REFERENCES cabinets(id) ON DELETE CASCADE
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


def match_cabinet(equipment_name):
    """关键词匹配柜子：双向子串匹配，返回匹配的 cabinet dict 或 None"""
    if not equipment_name:
        return None

    conn = get_db()
    cabinets = conn.execute(
        'SELECT id, cabinet_number, equipment_name FROM cabinets'
    ).fetchall()
    conn.close()

    name_lower = equipment_name.strip().lower()
    best_match = None
    best_len = 0

    for cab in cabinets:
        cab_name = cab['equipment_name'].strip().lower()
        if name_lower in cab_name or cab_name in name_lower:
            match_len = min(len(name_lower), len(cab_name))
            if match_len > best_len:
                best_match = dict(cab)
                best_len = match_len

    return best_match

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

    cabinets = conn.execute('''
        SELECT c.*, GROUP_CONCAT(cc.component_name, ', ') as components
        FROM cabinets c
        LEFT JOIN cabinet_components cc ON c.id = cc.cabinet_id
        GROUP BY c.id
        ORDER BY c.cabinet_number
    ''').fetchall()

    conn.close()

    applications_list = [dict(app) for app in applications]
    cabinets_list = [dict(c) for c in cabinets]

    return render_template('user.html',
                         applications=applications,
                         applications_json=json.dumps(applications_list),
                         cabinets=cabinets,
                         cabinets_json=json.dumps(cabinets_list),
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

    cabinets = conn.execute('''
        SELECT c.*, GROUP_CONCAT(cc.component_name, ', ') as components
        FROM cabinets c
        LEFT JOIN cabinet_components cc ON c.id = cc.cabinet_id
        GROUP BY c.id
        ORDER BY c.cabinet_number
    ''').fetchall()

    conn.close()

    applications_list = [dict(app) for app in applications]
    cabinets_list = [dict(c) for c in cabinets]

    return render_template('admin.html',
                         applications=applications,
                         applications_json=json.dumps(applications_list),
                         cabinets=cabinets,
                         cabinets_json=json.dumps(cabinets_list),
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
    cabinet_id = request.form.get('cabinet_id', '').strip()
    if not cabinet_id:
        cabinet_id = None
    expected_date = request.form.get('expected_date', '')
    
    if not purpose:
        return redirect(url_for('user_panel'))
    
    conn = get_db()
    conn.execute('''
        INSERT INTO applications 
        (user_id, equipment_name, quantity, photos, purpose, expected_date, cabinet_id) 
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (session['user_id'], equipment_name_str, quantity_str, photos_str, purpose, expected_date, cabinet_id))
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
# ==================== 接收ESP32指令 ====================
@app.route('/command', methods=['GET'])
def receive_command():
    """接收ESP32发来的器材名称，触发人脸验证"""
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
        print(f"🔍 请进行人脸验证...")
        print("="*60 + "\n")
        
        # 调用face.py进行人脸验证
        import subprocess
        face_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'face.py')
        subprocess.Popen(['python3', face_script, equipment_name])
        
        return f"OK, face verification started for: {equipment_name}", 200
        
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

@app.route('/api/face_verify', methods=['POST'])
def api_face_verify():
    """face.py调用：人脸验证并创建申请"""
    print("🔔 收到人脸验证请求")
    try:
        data = request.get_json()
        print(f"📝 数据: equipment={data.get('equipment_name')}")
        image_base64 = data.get('image', '')
        equipment_name = data.get('equipment_name', '')
        
        if not image_base64 or not equipment_name:
            return jsonify({'success': False, 'message': '缺少参数'})
        
        if 'base64,' in image_base64:
            image_base64 = image_base64.split('base64,')[1]
        
        # 调用百度AI搜索人脸
        result = face_client.search(
            image=image_base64,
            image_type='BASE64',
            group_id_list=GROUP_ID,
        )
        
        if result.get('error_code') != 0:
            return jsonify({'success': False, 'message': '人脸识别失败'})
        
        user_list = result.get('result', {}).get('user_list', [])
        if not user_list:
            return jsonify({'success': False, 'message': '未找到匹配人脸，请先注册'})
        
        top_user = user_list[0]
        score = float(top_user.get('score', 0))
        user_id = top_user.get('user_id', '')
        
        if score < 70:
            return jsonify({'success': False, 'message': f'匹配度过低({score:.1f})，请重试'})
        
        # 查找用户
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (int(user_id),)).fetchone()
        
        if not user:
            conn.close()
            return jsonify({'success': False, 'message': '用户不存在'})
        
        # 创建器材申请
        purpose = f"[人脸验证自动申请] 验证分数: {score:.1f}"
        conn.execute('''
            INSERT INTO applications 
            (user_id, equipment_name, quantity, purpose, status) 
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, equipment_name, '1', purpose, 'pending'))
        app_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 关键词匹配柜子
        cabinet = match_cabinet(equipment_name)
        if cabinet:
            conn.execute(
                'UPDATE applications SET cabinet_id = ? WHERE id = ?',
                (cabinet['id'], app_id)
            )
            print(f"🗄️ 自动匹配柜子: {cabinet['cabinet_number']}号柜 - {cabinet['equipment_name']}")
            pan_to_cabinet(cabinet['id'])

        conn.commit()
        conn.close()
        print(f"✅ 人脸验证: {user['username']} (分数:{score:.1f})")
        print(f"✅ 已创建申请: {equipment_name} (ID: #{app_id})")
        
        return jsonify({
            'success': True,
            'username': user['username'],
            'score': score,
            'application_id': app_id,
            'equipment': equipment_name
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


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
        
        app_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 关键词匹配柜子
        cabinet = match_cabinet(equipment_name)
        if cabinet:
            conn.execute(
                'UPDATE applications SET cabinet_id = ? WHERE id = ?',
                (cabinet['id'], app_id)
            )
            print(f"🗄️ 自动匹配柜子: {cabinet['cabinet_number']}号柜 - {cabinet['equipment_name']}")
            pan_to_cabinet(cabinet['id'])

        conn.commit()
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
@app.route('/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    """删除用户"""
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))
    
    # 不能删除自己
    if user_id == session['user_id']:
        return redirect(url_for('admin_panel'))
    
    conn = get_db()
    
    # 删除用户的申请记录
    conn.execute("DELETE FROM applications WHERE user_id = ?", (user_id,))
    # 删除用户
    conn.execute("DELETE FROM users WHERE id = ? AND username != 'admin'", (user_id,))
    
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_panel'))

# ==================== 器材柜子管理 ====================

@app.route('/api/cabinets')
def api_cabinets():
    """返回所有柜子列表（供下拉框使用）"""
    conn = get_db()
    cabinets = conn.execute('''
        SELECT c.*, GROUP_CONCAT(cc.component_name, ', ') as components
        FROM cabinets c
        LEFT JOIN cabinet_components cc ON c.id = cc.cabinet_id
        GROUP BY c.id
        ORDER BY c.cabinet_number
    ''').fetchall()
    conn.close()
    return jsonify([dict(c) for c in cabinets])

@app.route('/api/cabinet/<int:cabinet_id>')
def api_cabinet_detail(cabinet_id):
    """返回柜子详情：信息 + 元件 + 关联申请记录"""
    conn = get_db()
    cabinet = conn.execute('SELECT * FROM cabinets WHERE id = ?', (cabinet_id,)).fetchone()
    if not cabinet:
        conn.close()
        return jsonify({'success': False, 'message': '柜子不存在'})

    components = conn.execute(
        'SELECT * FROM cabinet_components WHERE cabinet_id = ?', (cabinet_id,)
    ).fetchall()

    applications = conn.execute('''
        SELECT a.*, u.username
        FROM applications a
        JOIN users u ON a.user_id = u.id
        WHERE a.cabinet_id = ?
        ORDER BY a.created_at DESC
    ''', (cabinet_id,)).fetchall()

    conn.close()
    return jsonify({
        'cabinet': dict(cabinet),
        'components': [dict(c) for c in components],
        'applications': [dict(a) for a in applications]
    })

@app.route('/cabinet/create', methods=['POST'])
def cabinet_create():
    """创建柜子"""
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))

    cabinet_number = request.form.get('cabinet_number', '').strip()
    equipment_name = request.form.get('equipment_name', '').strip()
    components = request.form.getlist('components[]')

    if not cabinet_number or not equipment_name:
        return redirect(url_for('admin_panel'))

    conn = get_db()
    conn.execute(
        'INSERT INTO cabinets (cabinet_number, equipment_name) VALUES (?, ?)',
        (cabinet_number, equipment_name)
    )
    cabinet_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for comp in components:
        comp = comp.strip()
        if comp:
            conn.execute(
                'INSERT INTO cabinet_components (cabinet_id, component_name) VALUES (?, ?)',
                (cabinet_id, comp)
            )

    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/cabinet/edit/<int:cabinet_id>', methods=['POST'])
def cabinet_edit(cabinet_id):
    """修改柜子"""
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))

    cabinet_number = request.form.get('cabinet_number', '').strip()
    equipment_name = request.form.get('equipment_name', '').strip()
    components = request.form.getlist('components[]')

    if not cabinet_number or not equipment_name:
        return redirect(url_for('admin_panel'))

    conn = get_db()
    conn.execute(
        'UPDATE cabinets SET cabinet_number = ?, equipment_name = ? WHERE id = ?',
        (cabinet_number, equipment_name, cabinet_id)
    )
    conn.execute('DELETE FROM cabinet_components WHERE cabinet_id = ?', (cabinet_id,))

    for comp in components:
        comp = comp.strip()
        if comp:
            conn.execute(
                'INSERT INTO cabinet_components (cabinet_id, component_name) VALUES (?, ?)',
                (cabinet_id, comp)
            )

    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/cabinet/delete/<int:cabinet_id>', methods=['POST'])
def cabinet_delete(cabinet_id):
    """删除单个柜子"""
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))

    conn = get_db()
    conn.execute('DELETE FROM cabinet_components WHERE cabinet_id = ?', (cabinet_id,))
    conn.execute('DELETE FROM cabinets WHERE id = ?', (cabinet_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_panel'))

@app.route('/cabinet/batch_delete', methods=['POST'])
def cabinet_batch_delete():
    """批量删除柜子"""
    if 'user_id' not in session or session.get('role') != 'admin':
        return redirect(url_for('login_page'))

    ids = request.form.getlist('ids[]')
    if ids:
        conn = get_db()
        for cid in ids:
            conn.execute('DELETE FROM cabinet_components WHERE cabinet_id = ?', (cid,))
            conn.execute('DELETE FROM cabinets WHERE id = ?', (cid,))
        conn.commit()
        conn.close()
    return redirect(url_for('admin_panel'))

if __name__ == '__main__':
    if not os.path.exists('templates'):
        os.makedirs('templates')
    if not os.path.exists('static'):
        os.makedirs('static')
    
    init_db()
    
    print("\n" + "="*50)
    print("🚀 器材申请管理系统启动成功！")
    print("📍 Flask 监听: 127.0.0.1:5050 (内部)")
    print("🌐 浏览器访问: https://<服务器IP>  (需先启动 Nginx)")
    print("📡 ESP32 访问: http://<服务器IP>/command")
    print("👤 管理员账号: admin / admin123")
    print("📷 人脸识别: 百度AI API")
    print("📁 上传文件目录: static/uploads/")
    print("="*50 + "\n")
    app.run(debug=True, host='127.0.0.1', port=5050)