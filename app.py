from string import ascii_uppercase
from datetime import datetime
import random
import json
import os

from flask_socketio import join_room, send, SocketIO
from flask import Flask, request, session, jsonify
from flask_cors import CORS

from pymongo import MongoClient
from redis import Redis

# Flask Config
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
CORS(app, supports_credentials=True)

# MongoDB configuration
mongo_client = MongoClient('mongodb://mongo:27017/chat_db')
db = mongo_client['chat_db']

# Redis configuration
redis_host = 'redis'
redis = Redis(host=redis_host, port=6379, decode_responses=True, password=os.getenv("REDIS_PASSWORD"))

# SocketIO config
socketio = SocketIO(app, cors_allowed_origins="*", message_queue=f'redis://redis:6379')

# Room-code generator
def generate_unique_code(length):
    """Generates a unique room code."""
    while True:
        code = "".join(random.choice(ascii_uppercase) for _ in range(length))
        if not redis.exists(f"room:{code}"):
            break
    return code

@app.route("/api/create-room", methods=["POST"])
def create_room():
    """API to create a new room."""
    name = request.json.get("name")
    if not name:
        return jsonify({"error": "Name is required"}), 404

    try:
        room_code = generate_unique_code(6)
        redis.hset(f"room:{room_code}", "members", json.dumps({}))
        redis.hset(f"room:{room_code}", "messages", json.dumps([]))
        session["room"] = room_code
        session["name"] = name
        return jsonify({"room": room_code, "name": name}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to create room: {str(e)}"}), 500

@app.route("/api/join-room", methods=["POST"])
def join_room_api():
    """API to join an existing room."""
    name = request.json.get("name")
    code = request.json.get("code")

    if not name:
        return jsonify({"error": "Name is required"}), 404
    if not redis.exists(f"room:{code}"):
        return jsonify({"error": "Room does not exist"}), 404

    try:
        session["room"] = code
        session["name"] = name
        return jsonify({"room": code, "name": name})
    except Exception as e:
        return jsonify({"error": f"Failed to join room: {str(e)}"}), 500

@app.route("/api/save-chat", methods=["POST"])
def save_chat():
    """API to save chat messages from Redis to MongoDB."""
    room = request.json.get("room")

    if not room:
        return jsonify({"error": "Room ID is required"}), 404
    if not redis.exists(f"room:{room}"):
        return jsonify({"error": "Room does not exist"}), 404

    try:
        previous_messages = json.loads(redis.hget(f"room:{room}", "messages") or "[]")
        chat_data = {
            "room_id": room,
            "messages": previous_messages,
        }
        db.chats.insert_one(chat_data)
        return jsonify({"message": "Chat saved successfully"}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to save chat: {str(e)}"}), 500

# SocketIO event handlers
@socketio.on("message")
def handle_message(data):
    """Handles sending and broadcasting messages to the room."""
    room = session.get("room")
    if not redis.exists(f"room:{room}"):
        return

    content = {
        "name": session.get("name"),
        "message": data["data"],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    # Broadcast the message to all users in the room
    send(content, to=room)

    # Save the message in Redis
    messages = json.loads(redis.hget(f"room:{room}", "messages"))
    messages.append(content)
    redis.hset(f"room:{room}", "messages", json.dumps(messages))

@socketio.on("connect")
def handle_connect():
    """Handles user connection and sends previous messages."""
    room = session.get("room")
    name = session.get("name")
    if not room or not name:
        return
    if not redis.exists(f"room:{room}"):
        return

    join_room(room)

    # Update member list in Redis
    members = json.loads(redis.hget(f"room:{room}", "members"))
    members[name] = True
    redis.hset(f"room:{room}", "members", json.dumps(members))

    # Notify the room about the new user and send the updated member list
    send({"members": list(members.keys())}, to=request.sid)
    socketio.emit('members', {"members": list(members.keys())}, to=room)

    # Send previous messages to the newly connected user
    previous_messages = json.loads(redis.hget(f"room:{room}", "messages"))
    socketio.emit('previous-messages', {"messages": previous_messages}, to=request.sid)

@socketio.on("disconnect")
def handle_disconnect():
    """Handles user disconnection and removes them from the member list."""
    room = session.get("room")
    name = session.get("name")
    if not room or not name:
        return
    if not redis.exists(f"room:{room}"):
        return

    # Remove the user from the member list in Redis
    members = json.loads(redis.hget(f"room:{room}", "members"))
    if name in members:
        del members[name]
        redis.hset(f"room:{room}", "members", json.dumps(members))

        # Notify the room about the user leaving
        socketio.emit('members', {"members": list(members.keys())}, to=room)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
