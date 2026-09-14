import os
import random
import string
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = '363636M1nhqaauzn'

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    max_http_buffer_size=10 * 1024 * 1024,
    async_mode='threading'
)

ROOMS = {}

def generate_digit_code(length=6):
    return ''.join(random.choices(string.digits, k=length))

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
    questions = data.get('questions', [])
    pin = generate_digit_code(6)
    
    # Đảm bảo PIN không trùng
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
    
    # Kiểm tra Game PIN có tồn tại không
    if input_pin not in ROOMS:
        emit('admin_login_error', {
            'message': 'Game PIN không tồn tại! Phòng có thể đã kết thúc.'
        })
        print(f'❌ Admin login failed: PIN {input_pin} not found')
        return
    
    room = ROOMS[input_pin]
    
    # Kiểm tra Admin PIN có chính xác không
    if input_admin_pin != room['admin_pin']:
        emit('admin_login_error', {
            'message': 'Admin PIN không chính xác!'
        })
        print(f'❌ Admin login failed: Wrong admin PIN for {input_pin}')
        return
    
    # Đăng nhập thành công - cập nhật host_sid mới
    room['host_sid'] = request.sid
    join_room(input_pin)
    
    emit('admin_login_success', {
        'pin': input_pin,
        'admin_pin': room['admin_pin'],
        'questions': room['questions'],
        'current_q_index': room['current_q_index'],
        'state': room['state']
    })
    
    # Gửi danh sách người chơi hiện tại
    send_player_update(input_pin)
    
    print(f'✅ Admin login success: PIN={input_pin}')

@socketio.on('join_room')
def handle_join_room(data):
    """Player tham gia phòng game bằng Game PIN"""
    input_code = str(data.get('pin', '')).strip()
    nickname = str(data.get('nickname', '')).strip()
    
    # Kiểm tra PIN có tồn tại không
    if input_code not in ROOMS:
        emit('join_error', {'message': 'Mã PIN không tồn tại!'})
        print(f'❌ Join failed: PIN {input_code} not found')
        return
    
    room = ROOMS[input_code]
    
    # Kiểm tra trạng thái phòng
    if room['state'] != 'lobby':
        emit('join_error', {'message': 'Trò chơi đã bắt đầu! Không thể vào lúc này.'})
        print(f'❌ Join failed: Room {input_code} already started')
        return
    
    # Thêm player vào phòng
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
    
    # Cập nhật danh sách người chơi cho admin
    send_player_update(input_code)
    
    print(f'✅ Player joined: {nickname} in room {input_code}')

@socketio.on('next_question')
def handle_next_question(data):
    """Admin bấn nút chuyển câu hỏi"""
    pin = data.get('pin')
    
    if pin not in ROOMS:
        emit('error', {'message': 'Phòng không tồn tại'})
        return
    
    room = ROOMS[pin]
    
    # Kiểm tra có phải host không
    if room['host_sid'] != request.sid:
        emit('error', {'message': 'Bạn không phải là host!'})
        return
    
    room['current_q_index'] += 1
    room['answered_players'].clear()
    
    # Kiểm tra hết câu hỏi chưa
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
        
        # Reset trạng thái answered của tất cả players
        for player in room['players'].values():
            player['answered'] = False
        
        # Lấy câu hỏi hiện tại
        q = room['questions'][room['current_q_index']]
        q_data = {
            'index': room['current_q_index'] + 1,
            'total': len(room['questions']),
            'title': q['title'],
            'type': q['type'],
            'image': q.get('image', ''),
            'options': q.get('options', [])
        }
        
        socketio.emit('new_question', q_data, to=pin)
        print(f'📝 Question {q_data["index"]}/{q_data["total"]} sent to room {pin}')

@socketio.on('submit_answer')
def handle_submit_answer(data):
    """Player gửi câu trả lời"""
    pin = data.get('pin')
    answer = str(data.get('answer', '')).strip()
    
    if pin not in ROOMS:
        return
    
    room = ROOMS[pin]
    
    if request.sid not in room['players']:
        return
    
    player = room['players'][request.sid]
    
    # Kiểm tra player đã trả lời chưa
    if player['answered']:
        return
    
    player['answered'] = True
    room['answered_players'].add(request.sid)
    
    # Lấy câu hỏi hiện tại
    current_q = room['questions'][room['current_q_index']]
    
    # So sánh câu trả lời (không phân biệt hoa thường)
    if current_q['type'] == 'quiz':
        correct_ans = str(current_q['correct']).strip()
        is_correct = (str(answer) == str(correct_ans))
    else:
        correct_ans = str(current_q['correct']).strip().lower()
        is_correct = (answer.lower() == correct_ans)
    
    # Cộng điểm nếu đúng
    if is_correct:
        player['score'] += 10
    
    # Gửi kết quả cho player
    emit('answer_result', {
        'correct': is_correct, 
        'score': player['score']
    })
    
    # Cập nhật danh sách player cho admin (để admin thấy ai đã trả lời)
    send_player_update(pin)
    
    # Phát event cho admin biết có player trả lời
    socketio.emit('player_submitted', {
        'player_id': request.sid,
        'player_name': player['name'],
        'answered_count': len(room['answered_players']),
        'total_players': len(room['players'])
    }, to=pin)
    
    print(f'📤 Answer submitted by {player["name"]} - Correct: {is_correct}')

@socketio.on('kick_player')
def handle_kick_player(data):
    """Admin kick player khỏi phòng"""
    pin = data.get('pin')
    player_id = data.get('player_id')
    
    if pin not in ROOMS:
        return
    
    room = ROOMS[pin]
    
    # Kiểm tra có phải host không
    if room['host_sid'] != request.sid:
        return
    
    # Xóa player khỏi phòng
    if player_id in room['players']:
        del room['players'][player_id]
        room['answered_players'].discard(player_id)
        
        # Thông báo cho player bị kick
        socketio.emit('kicked', to=player_id)
        
        # Cập nhật danh sách player cho admin
        send_player_update(pin)
        
        print(f'🗑️ Player {player_id} kicked from room {pin}')

@socketio.on('trigger_admin_dev')
def handle_trigger_admin(data):
    """Admin bấn nút Dev Signature"""
    pin = data.get('pin')
    
    if pin in ROOMS and ROOMS[pin]['host_sid'] == request.sid:
        # Phát dev signature cho tất cả người dùng trong phòng
        socketio.emit('show_dev_sig', to=pin)
        print(f'✨ Dev signature triggered in room {pin}')

@socketio.on('adjust_score')
def handle_adjust_score(data):
    """Admin điều chỉnh điểm của player"""
    pin = data.get('pin')
    player_id = data.get('player_id')
    delta = data.get('delta', 0)
    
    if pin not in ROOMS:
        return
    
    room = ROOMS[pin]
    
    # Kiểm tra có phải host không
    if room['host_sid'] != request.sid:
        return
    
    # Điều chỉnh điểm
    if player_id in room['players']:
        room['players'][player_id]['score'] += delta
        send_player_update(pin)
        print(f'📊 Score adjusted for player {player_id}: {delta:+d}')

@socketio.on('disconnect')
def handle_disconnect():
    """Xử lý khi client ngắt kết nối"""
    print(f'❌ Client disconnected: {request.sid}')
    
    for pin, room in list(ROOMS.items()):
        # Nếu host ngắt kết nối, giữ lại phòng để host có thể đăng nhập lại
        if room['host_sid'] == request.sid:
            room['host_sid'] = None
            print(f'⚠️ Host disconnected from room {pin} - room preserved for reconnect')
            break
        
        # Nếu player ngắt kết nối, xóa player khỏi phòng
        elif request.sid in room['players']:
            player_name = room['players'][request.sid]['name']
            del room['players'][request.sid]
            room['answered_players'].discard(request.sid)
            send_player_update(pin)
            print(f'⚠️ Player {player_name} disconnected from room {pin}')
            break

# --- HELPER FUNCTIONS ---

def send_player_update(pin):
    """Gửi danh sách người chơi hiện tại cho tất cả trong phòng"""
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
