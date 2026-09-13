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

@socketio.on('create_room')
def handle_create_room(data):
    questions = data.get('questions', [])
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
        'state': 'lobby'
    }
    join_room(pin)
    emit('room_created', {
        'pin': pin, 
        'admin_pin': admin_pin, 
        'total_questions': len(questions)
    })

@socketio.on('join_room')
def handle_join_room(data):
    input_code = str(data.get('pin', '')).strip()
    nickname = str(data.get('nickname', '')).strip()
    
    # 1. Kiểm tra nếu người dùng nhập mã Admin PIN để khôi phục quyền Host
    for pin, room in ROOMS.items():
        if input_code == room['admin_pin']:
            room['host_sid'] = request.sid
            join_room(pin)
            emit('admin_reconnect_success', {
                'pin': pin,
                'admin_pin': room['admin_pin'],
                'questions': room['questions']
            })
            send_player_update(pin)
            return

    # 2. Kiểm tra nếu nhập Game PIN người chơi bình thường
    if input_code not in ROOMS:
        emit('join_error', {'message': 'Mã PIN không tồn tại!'})
        return
        
    if ROOMS[input_code]['state'] != 'lobby':
        emit('join_error', {'message': 'Trò chơi đã bắt đầu!'})
        return

    ROOMS[input_code]['players'][request.sid] = {
        'name': nickname,
        'score': 0,
        'answered': False
    }
    join_room(input_code)
    
    emit('join_success', {'pin': input_code, 'nickname': nickname})
    send_player_update(input_code)

@socketio.on('next_question')
def handle_next_question(data):
    pin = data.get('pin')
    if pin in ROOMS and ROOMS[pin]['host_sid'] == request.sid:
        room = ROOMS[pin]
        room['current_q_index'] += 1
        
        if room['current_q_index'] >= len(room['questions']):
            room['state'] = 'ended'
            leaderboard = sorted(room['players'].values(), key=lambda x: x['score'], reverse=True)
            socketio.emit('game_over', {'leaderboard': leaderboard}, to=pin)
        else:
            room['state'] = 'playing'
            for p in room['players'].values():
                p['answered'] = False
                
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

@socketio.on('submit_answer')
def handle_submit_answer(data):
    pin = data.get('pin')
    answer = str(data.get('answer', '')).strip().lower()
    
    if pin in ROOMS and request.sid in ROOMS[pin]['players']:
        player = ROOMS[pin]['players'][request.sid]
        if player['answered']:
            return
            
        player['answered'] = True
        current_q = ROOMS[pin]['questions'][ROOMS[pin]['current_q_index']]
        correct_ans = str(current_q['correct']).strip().lower()
        
        is_correct = (answer == correct_ans)
        if is_correct:
            player['score'] += 10
            
        emit('answer_result', {'correct': is_correct, 'score': player['score']})
        send_player_update(pin)

@socketio.on('kick_player')
def handle_kick_player(data):
    pin = data.get('pin')
    player_id = data.get('player_id')
    if pin in ROOMS and ROOMS[pin]['host_sid'] == request.sid:
        if player_id in ROOMS[pin]['players']:
            del ROOMS[pin]['players'][player_id]
            emit('kicked', to=player_id)
            send_player_update(pin)

@socketio.on('trigger_admin_dev')
def handle_trigger_admin(data):
    pin = data.get('pin')
    if pin in ROOMS and ROOMS[pin]['host_sid'] == request.sid:
        socketio.emit('show_dev_sig', to=pin)

@socketio.on('disconnect')
def handle_disconnect():
    for pin, room in list(ROOMS.items()):
        # Xóa sid của host khi ngắt kết nối nhưng giữ lại phòng để Host có thể dùng Admin PIN vào lại
        if room['host_sid'] == request.sid:
            room['host_sid'] = None
            break
        elif request.sid in room['players']:
            del room['players'][request.sid]
            send_player_update(pin)
            break

# --- HELPER FUNCTIONS ---

def send_player_update(pin):
    if pin in ROOMS:
        players_data = [{'id': sid, 'name': p['name'], 'score': p['score']} for sid, p in ROOMS[pin]['players'].items()]
        socketio.emit('update_player_list', {'players': players_data}, to=pin)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
