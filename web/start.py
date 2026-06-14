# python3 -m venv venv 先开虚拟环境
# pip install flask flask-cors baidu-aip chardet requests urllib3 -i 
# https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
# 前置库安装
# source venv/bin/activate
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, jsonify
from aip import AipFace
import base64
import sqlite3
import hashlib
import os
import json
import time
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
_hardware_lock = threading.Lock()

try:
    from agent_yuntai.panTilt import lookAt, turnLED, x_motor, y_motor
    import picamera2
    import apriltag
    _pan_tilt_ready = True
    print("✅ 云台模块已就绪（相机延迟初始化）")
except Exception as e:
    _pan_tilt_ready = False
    print(f"⚠️ 云台模块不可用（非树莓派环境或缺少依赖）{e}")

_laser_delay_seconds = 3.0   # 激光持续亮灯秒数，后台可调
_face_pan_angle = 0.0         # 人脸识别水平转角（度）
_face_tilt_angle = 0.0        # 人脸识别垂直转角（度）


def _turn_to_face():
    """云台转到人脸识别位置（调用线程阻塞，~ms级）"""
    if _pan_tilt_ready and (_face_pan_angle != 0 or _face_tilt_angle != 0):
        with _hardware_lock:
            x_motor.turnAngle(abs(_face_pan_angle), 1 if _face_pan_angle < 0 else 0)
            y_motor.turnAngle(abs(_face_tilt_angle), 1 if _face_tilt_angle < 0 else 0)
        print(f"🔄 云台转到人脸位置 pan={_face_pan_angle}° tilt={_face_tilt_angle}°")


def _turn_back_from_face():
    """从人脸位置转回原位"""
    if _pan_tilt_ready and (_face_pan_angle != 0 or _face_tilt_angle != 0):
        with _hardware_lock:
            x_motor.turnAngle(abs(_face_pan_angle), 0 if _face_pan_angle < 0 else 1)
            y_motor.turnAngle(abs(_face_tilt_angle), 0 if _face_tilt_angle < 0 else 1)
        print("↩️ 云台转回原位")


def _pan_to_cabinet_sync(cabinet_id):
    """同步执行云台指向（在独立线程中调用）"""
    global _picam2, _detector
    with _hardware_lock:
        try:
            if _picam2 is None:
                _picam2 = picamera2.Picamera2()
                _picam2.configure(_picam2.create_preview_configuration(main={"size": (3280, 2464)}))
                _detector = apriltag.apriltag("tag36h11", threads=4)
                print("📷 相机首次初始化完成")
            _picam2.start()
            turnLED(True)                                        # 开激光
            lookAt(cabinet_id, _picam2, _detector)              # 阻塞，直到对准
            time.sleep(_laser_delay_seconds)                    # 对准后持续亮灯
            turnLED(False)                                       # 关激光
        except Exception:
            turnLED(False)                                       # 异常也要关
            raise
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


@app.route('/api/laser_delay', methods=['GET', 'POST'])
def api_laser_delay():
    """获取或设置激光延时（秒）"""
    global _laser_delay_seconds
    if 'user_id' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'unauthorized'}), 403
    if request.method == 'GET':
        return jsonify({'delay': _laser_delay_seconds})
    _laser_delay_seconds = float(request.form.get('delay', 3.0))
    print(f"⚙️ 激光延时已更新为 {_laser_delay_seconds} 秒")
    return redirect(url_for('admin_panel'))


@app.route('/api/face_angles', methods=['GET', 'POST'])
def api_face_angles():
    """获取或设置人脸识别云台角度"""
    global _face_pan_angle, _face_tilt_angle
    if 'user_id' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'unauthorized'}), 403
    if request.method == 'GET':
        return jsonify({'pan': _face_pan_angle, 'tilt': _face_tilt_angle})
    _face_pan_angle = float(request.form.get('pan', 0))
    _face_tilt_angle = float(request.form.get('tilt', 0))
    print(f"⚙️ 人脸角度已更新 pan={_face_pan_angle}° tilt={_face_tilt_angle}°")
    return redirect(url_for('admin_panel'))
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

# ==================== ESP32 ====================

try:
    from face import capture_photo as face_capture
except Exception:
    face_capture = None


def _esp32_request_thread(equipment_name):
    global _detector
    picam2 = None
    try:
        with _hardware_lock:
            _turn_to_face()
            if _pan_tilt_ready and face_capture:
                picam2 = picamera2.Picamera2()
                picam2.configure(picam2.create_still_configuration(
                    main={"size": (640, 480), "format": "RGB888"}
                ))
                picam2.start()
                time.sleep(0.5)
                image = face_capture(picam2)
                picam2.stop()
            else:
                print("Camera not available")
                return

        result = face_client.search(
            image=image, image_type='BASE64', group_id_list=GROUP_ID
        )
        if result.get('error_code') != 0:
            print(f"Face search failed: {result.get('error_msg')}")
            return

        user_list = result.get('result', {}).get('user_list', [])
        if not user_list:
            print("No matching face found")
            return

        top_user = user_list[0]
        score = float(top_user.get('score', 0))
        user_id = top_user.get('user_id', '')
        if score < 70:
            print(f"Score too low ({score:.1f})")
            return

        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (int(user_id),)).fetchone()
        if not user:
            conn.close()
            print("User not found")
            return

        purpose = f"[Auto] Score: {score:.1f}"
        conn.execute(
            "INSERT INTO applications"
            " (user_id, equipment_name, quantity, purpose, status)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, equipment_name, '1', purpose, 'pending')
        )
        app_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        cabinet = match_cabinet(equipment_name)
        if cabinet:
            conn.execute(
                'UPDATE applications SET cabinet_id = ? WHERE id = ?',
                (cabinet['id'], app_id)
            )
        conn.commit()
        conn.close()

        print(f"Face OK: {user['username']} (score:{score:.1f})")
        print(f"App created: {equipment_name} (ID: #{app_id})")

        with _hardware_lock:
            _turn_back_from_face()
            if cabinet and _pan_tilt_ready:
                if _detector is None:
                    _detector = apriltag.apriltag("tag36h11", threads=4)
                picam2.configure(picam2.create_preview_configuration(main={"size": (3280, 2464)}))
                picam2.start()
                try:
                    turnLED(True)
                    print(f"PanTilt to tag_id={cabinet['id']}")
                    lookAt(cabinet['id'], picam2, _detector)
                    time.sleep(_laser_delay_seconds)
                    turnLED(False)
                except Exception:
                    turnLED(False)
                    raise
                finally:
                    picam2.stop()

    except Exception as e:
        print(f"ESP32 request error: {e}")
    finally:
        if picam2:
            try:
                picam2.stop()
            except Exception:
                pass


@app.route('/command', methods=['GET'])
def receive_command():
    equipment_name = request.args.get('cmd', '')
    equipment_name = urllib.parse.unquote(equipment_name)
    if not equipment_name:
        return "ERROR: Empty command", 400
    client_ip = request.remote_addr
    receive_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print("\n" + "="*60)
    print(f"ESP32 request from {client_ip}")
    print(f"Time: {receive_time}")
    print(f"Equipment: {equipment_name}")
    print("="*60 + "\n")
    threading.Thread(
        target=_esp32_request_thread, args=(equipment_name,),
        daemon=True
    ).start()
    return f"OK: {equipment_name}", 200

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
        # 云台转到人脸识别位置
        _turn_to_face()

        
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

        # 转回原位
        _turn_back_from_face()

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