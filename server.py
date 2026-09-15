import os
import random
import string
import base64
from io import BytesIO
from PIL import Image, ImageSequence
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = '363636M1nhqaauzn'

# 1. Cấu hình SocketIO tối ưu Performance
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    max_http_buffer_size=100 * 1024 * 1024, # Nâng buffer size lên 100MB cho dữ liệu GIF
    async_mode='gevent', # Khuyên dùng gevent hoặc eventlet thay cho threading để tránh lag Socket
    ping_timeout=60,
    ping_interval=25
)

ROOMS = {}

def generate_digit_code(length=6):
    return ''.join(random.choices(string.digits, k=length))

def optimize_gif_base64(b64_string, max_size=(600, 600), quality=70):
    """
    Hàm helper tự động resize & nén GIF Base64 trực tiếp trên Server 
    để giảm dung lượng từ 80-90% trước khi gửi xuống Client.
    """
    if not b64_string or not b64_string.startswith('data:image'):
        return b64_string
    
    try:
        header, encoded = b64_string.split(',', 1)
        image_data = base64.b64decode(encoded)
        img = Image.open(BytesIO(image_data))

        # Nếu là GIF động
        if getattr(img, "is_animated", False):
            frames = []
            for frame in ImageSequence.Iterator(img):
                f = frame.copy()
                f.thumbnail(max_size, Image.Resampling.LANCZOS)
                frames.append(f)
            
            output = BytesIO()
            frames[0].save(
                output,
                format='GIF',
                save_all=True,
                append_images=frames[1:],
                optimize=True,
                loop=0
            )
            compressed_b64 = base64.b64encode(output.getvalue()).decode('utf-8')
            return f"{header},{compressed_b64}"
        else:
            # Nếu là ảnh tĩnh (PNG/JPG)
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            output = BytesIO()
            img.save(output, format='WEBP', quality=quality, optimize=True)
            compressed_b64 = base64.b64encode(output.getvalue()).decode('utf-8')
            return f"data:image/webp;base64,{compressed_b64}"
            
    except Exception as e:
        print(f"⚠️ Warning: Compression failed, using original media. Error: {e}")
        return b64_string


@app.route('/healthcheck')
@app.route('/ping')
def healthcheck():
    return {'status': 'online', 'active_rooms': len(ROOMS)}, 200

# --- SOCKET EVENTS ---

@socketio.on('connect')
def handle_connect():
    print(f'✅ Client connected: {request.sid}')

@socketio.on('create_room')
def handle_create_room(data):
    """Admin tạo phòng game mới"""
    raw_questions = data.get('questions', [])
    
    # Nén toàn bộ ảnh/GIF ngay khi tạo phòng để tránh nghẽn khi đang chơi
    questions = []
    for q in raw_questions:
        if q.get('image'):
            q['image'] = optimize_gif_base64(q['image'])
        questions.append(q)

    pin = generate_digit_code(6)
    while pin in ROOMS:
        pin = generate_digit_code(6)
    
    admin_pin = "ADM-" + generate_digit_code(6)
    
    ROOMS[pin] = {
        'admin_pin': admin_pin,
        'host_sid': request.sid,
        'players': {},
        'questions': questions,
        'current_q_index': -1,
        'state': 'lobby',
        'answered_players': set()
    }
    
    join_room(pin)
    
    emit('room_created', {
        'pin': pin, 
        'admin_pin': admin_pin, 
        'total_questions': len(questions)
    })
    
    print(f'🎮 Room created: PIN={pin}, ADMIN_PIN={admin_pin}')

@socketio.on('admin_login')
def handle_admin_login(data):
    """Admin đăng nhập lại vào phòng game đã tạo trước đó"""
    input_pin = str(data.get('pin', '')).strip()
    input_admin_pin = str(data.get('admin_pin', '')).strip()
    
    if input_pin not in ROOMS:
        emit('admin_login_error', {'message': 'Game PIN không tồn tại!'})
        return
    
    room = ROOMS[input_pin]
    
    if input_admin_pin != room['admin_pin']:
        emit('admin_login_error', {'message': 'Admin PIN không chính xác!'})
        return
    
    room['host_sid'] = request.sid
    join_room(input_pin)
    
    emit('admin_login_success', {
        'pin': input_pin,
        'admin_pin': room['admin_pin'],
        'questions': room['questions'],
        'current_q_index': room['current_q_index'],
        'state': room['state']
    })
    
    send_player_update(input_pin)
    print(f'✅ Admin login success: PIN={input_pin}')

@socketio.on('join_room')
def handle_join_room(data):
    """Player tham gia phòng game"""
    input_code = str(data.get('pin', '')).strip()
    nickname = str(data.get('nickname', '')).strip()
    
    if input_code not in ROOMS:
        emit('join_error', {'message': 'Mã PIN không tồn tại!'})
        return
    
    room = ROOMS[input_code]
    
    if room['state'] != 'lobby':
        emit('join_error', {'message': 'Trò chơi đã bắt đầu!'})
        return
    
    ROOMS[input_code]['players'][request.sid] = {
        'name': nickname,
        'score': 0,
        'answered': False
    }
    
    join_room(input_code)
    
    emit('join_success', {
        'pin': input_code, 
        'nickname': nickname
    })
    
    send_player_update(input_code)
    print(f'✅ Player joined: {nickname} in room {input_code}')

@socketio.on('next_question')
def handle_next_question(data):
    """Admin bấm chuyển câu hỏi"""
    pin = data.get('pin')
    
    if pin not in ROOMS:
        emit('error', {'message': 'Phòng không tồn tại'})
        return
    
    room = ROOMS[pin]
    
    if room['host_sid'] != request.sid:
        emit('error', {'message': 'Bạn không phải là host!'})
        return
    
    room['current_q_index'] += 1
    room['answered_players'].clear()
    
    if room['current_q_index'] >= len(room['questions']):
        room['state'] = 'ended'
        leaderboard = sorted(
            room['players'].items(),
            key=lambda x: x[1]['score'],
            reverse=True
        )
        leaderboard_data = [
            {'name': p[1]['name'], 'score': p[1]['score']} 
            for p in leaderboard
        ]
        socketio.emit('game_over', {'leaderboard': leaderboard_data}, to=pin)
        print(f'🏆 Game ended in room {pin}')
    else:
        room['state'] = 'playing'
        
        for player in room['players'].values():
            player['answered'] = False
        
        q = room['questions'][room['current_q_index']]
        q_data = {
            'index': room['current_q_index'] + 1,
            'total': len(room['questions']),
            'title': q['title'],
            'type': q['type'],
            'image': q.get('image', ''), # Chỉ gửi câu hỏi hiện tại kèm ảnh/GIF đã nén
            'options': q.get('options', [])
        }
        
        socketio.emit('new_question', q_data, to=pin)
        print(f'📝 Question {q_data["index"]}/{q_data["total"]} sent to room {pin}')

@socketio.on('submit_answer')
def handle_submit_answer(data):
    pin = data.get('pin')
    answer = str(data.get('answer', '')).strip()
    
    if pin not in ROOMS or request.sid not in ROOMS[pin]['players']:
        return
    
    room = ROOMS[pin]
    player = room['players'][request.sid]
    
    if player['answered']:
        return
    
    player['answered'] = True
    room['answered_players'].add(request.sid)
    
    current_q = room['questions'][room['current_q_index']]
    
    if current_q['type'] == 'quiz':
        correct_ans = str(current_q['correct']).strip()
        is_correct = (str(answer) == str(correct_ans))
    else:
        correct_ans = str(current_q['correct']).strip().lower()
        is_correct = (answer.lower() == correct_ans)
    
    if is_correct:
        player['score'] += 10
    
    emit('answer_result', {
        'correct': is_correct, 
        'score': player['score']
    })
    
    send_player_update(pin)
    
    socketio.emit('player_submitted', {
        'player_id': request.sid,
        'player_name': player['name'],
        'answered_count': len(room['answered_players']),
        'total_players': len(room['players'])
    }, to=pin)

@socketio.on('kick_player')
def handle_kick_player(data):
    pin = data.get('pin')
    player_id = data.get('player_id')
    
    if pin not in ROOMS or ROOMS[pin]['host_sid'] != request.sid:
        return
    
    room = ROOMS[pin]
    if player_id in room['players']:
        del room['players'][player_id]
        room['answered_players'].discard(player_id)
        socketio.emit('kicked', to=player_id)
        send_player_update(pin)

@socketio.on('trigger_admin_dev')
def handle_trigger_admin(data):
    pin = data.get('pin')
    if pin in ROOMS and ROOMS[pin]['host_sid'] == request.sid:
        socketio.emit('show_dev_sig', to=pin)

@socketio.on('adjust_score')
def handle_adjust_score(data):
    pin = data.get('pin')
    player_id = data.get('player_id')
    delta = data.get('delta', 0)
    
    if pin in ROOMS and ROOMS[pin]['host_sid'] == request.sid:
        if player_id in ROOMS[pin]['players']:
            ROOMS[pin]['players'][player_id]['score'] += delta
            send_player_update(pin)

@socketio.on('disconnect')
def handle_disconnect():
    print(f'❌ Client disconnected: {request.sid}')
    for pin, room in list(ROOMS.items()):
        if room['host_sid'] == request.sid:
            room['host_sid'] = None
            break
        elif request.sid in room['players']:
            del room['players'][request.sid]
            room['answered_players'].discard(request.sid)
            send_player_update(pin)
            break

def send_player_update(pin):
    if pin in ROOMS:
        players_data = [
            {
                'id': sid, 
                'name': p['name'], 
                'score': p['score'],
                'answered': p['answered']
            } 
            for sid, p in ROOMS[pin]['players'].items()
        ]
        socketio.emit('update_player_list', {'players': players_data}, to=pin)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'🚀 Starting server on port {port}...')
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
