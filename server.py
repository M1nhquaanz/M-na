import os
import random
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'kahoot_ultra_secret_key'

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    max_http_buffer_size=10 * 1024 * 1024,
    async_mode='threading'
)

ROOMS = {}

# Route kiểm tra Server live trên Render
@app.route('/healthcheck')
@app.route('/ping')
def healthcheck():
    return {'status': 'online', 'active_rooms': len(ROOMS)}, 200

# --- SOCKET EVENTS ---

@socketio.on('create_room')
def handle_create_room(data):
    questions = data.get('questions', [])
    pin = str(random.randint(100000, 999999))
    while pin in ROOMS:
        pin = str(random.randint(100000, 999999))
    
    ROOMS[pin] = {
        'host_sid': request.sid,
        'players': {},
        'questions': questions,
        'current_q_index': -1,
        'state': 'lobby'
    }
    join_room(pin)
    emit('room_created', {'pin': pin, 'total_questions': len(questions)})

@socketio.on('join_room')
def handle_join_room(data):
    pin = data.get('pin')
    nickname = data.get('nickname')
    
    if pin not in ROOMS:
        emit('join_error', {'message': 'Mã PIN không tồn tại!'})
        return
        
    if ROOMS[pin]['state'] != 'lobby':
        emit('join_error', {'message': 'Trò chơi đã bắt đầu!'})
        return

    ROOMS[pin]['players'][request.sid] = {
        'name': nickname,
        'score': 0,
        'answered': False
    }
    join_room(pin)
    
    emit('join_success', {'pin': pin, 'nickname': nickname})
    send_player_update(pin)

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
        if room['host_sid'] == request.sid:
            socketio.emit('host_left', {'message': 'Host đã rời phòng!'}, to=pin)
            del ROOMS[pin]
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
    socketio.run(app, host='0.0.0.0', port=port)
