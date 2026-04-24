import os
import uuid
from datetime import datetime
from dotenv import load_dotenv

from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.adapters.schemas.tools_schema import ToolsSchema 
from pipecat.adapters.schemas.function_schema import FunctionSchema

from pipecat.frames.frames import (
    LLMMessagesUpdateFrame, 
    AudioRawFrame, 
    OutputAudioRawFrame, 
    TextFrame
)
from pipecat.serializers.base_serializer import FrameSerializer
from database import get_db
from config import SYSTEM_PROMPT

load_dotenv()


def calculate_distance(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return 999999.0
    from math import radians, sin, cos, acos
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    return acos(sin(lat1) * sin(lat2) + cos(lat1) * cos(lat2) * cos(lng2 - lng1)) * 6371


def determine_city(lat: float, lng: float) -> str:
    # Simple bounding box check
    if 12.8 <= lat <= 13.1 and 77.4 <= lng <= 77.8:
        return "Bangalore"
    elif 9.8 <= lat <= 10.1 and 76.1 <= lng <= 76.4:
        return "Kochi"
    else:
        return "Unknown"


class DatabaseProcessor(FrameProcessor):
    def __init__(self, user_id, incident_id, initial_lat=None, initial_lng=None):
        super().__init__()
        self.user_id = user_id
        self.incident_id = incident_id
        self.initial_lat = initial_lat
        self.initial_lng = initial_lng
        self.created = False

    async def create_incident_record(self, text):
        conn = get_db()
        try:
            conn.execute('''
                INSERT OR IGNORE INTO incidents (id, user_id, transcript, status, lat, lng, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                self.incident_id,
                self.user_id,
                text,
                "voice_active",
                self.initial_lat,
                self.initial_lng,
                datetime.now().isoformat()
            ))
            conn.commit()
            self.created = True
        finally:
            conn.close()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame):
            if not self.created:
                await self.create_incident_record(frame.text)

            conn = get_db()
            try:
                role = "assistant" if direction == "output" else "user"
                conn.execute('''
                    INSERT INTO transcripts (incident_id, user_id, role, text, created_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    self.incident_id,
                    self.user_id,
                    role,
                    frame.text,
                    datetime.now().isoformat()
                ))
                conn.execute('''
                    UPDATE incidents SET transcript = ?, updated_at = ? WHERE id = ?
                ''', (
                    frame.text,
                    datetime.now().isoformat(),
                    self.incident_id
                ))
                conn.commit()
            finally:
                conn.close()


class AudioSerializer(FrameSerializer):
    async def serialize(self, frame) -> str | bytes | None:
        if isinstance(frame, (AudioRawFrame, OutputAudioRawFrame)):
            return frame.audio
        return None

    async def deserialize(self, data: str | bytes):
        frame = AudioRawFrame(audio=data, sample_rate=16000, num_channels=1)
        setattr(frame, "id", str(uuid.uuid4()))
        setattr(frame, "timestamp", datetime.now().isoformat())
        setattr(frame, "broadcast_sibling_id", None)
        return frame


async def run_emergency_bot(websocket, user_id, incident_id, device_lat=None, device_lng=None, initial_history=[]):
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_out_enabled=True,
            add_wav_header=True,
            audio_out_sample_rate=16000,
            serializer=AudioSerializer()
        )
    )

    stt = OpenAISTTService(api_key=os.getenv("OPENAI_API_KEY"), model="whisper-1")
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-5 nano")
    tts = OpenAITTSService(api_key=os.getenv("OPENAI_API_KEY"), voice="alloy")

    update_incident_tool = FunctionSchema(
        name="update_incident_details",
        description="Categorize the aid request and assign the nearest emergency resource based on user's location.",
        properties={
            "category": {"type": "string", "enum": ["Fire", "Medical", "Security", "General"]},
            "severity": {"type": "string", "enum": ["Low", "Medium", "High", "Critical"]},
            "distress_level": {"type": "integer", "minimum": 1, "maximum": 10},
            "user_name": {"type": "string"},
            "location_details": {"type": "string"}
        },
        required=["category", "severity", "distress_level"]
    )

    tools = ToolsSchema(standard_tools=[update_incident_tool])

    full_prompt = SYSTEM_PROMPT
    if device_lat is not None and device_lng is not None:
        full_prompt += f"\nNote: Real-time GPS coordinates verified at {device_lat}, {device_lng}."

    messages = [{"role": "system", "content": full_prompt}]
    messages.extend(initial_history)

    context = LLMContext(messages, tools)
    context_aggregator = LLMContextAggregatorPair(context)
    db_processor = DatabaseProcessor(user_id, incident_id, device_lat, device_lng)

    def assign_nearest_unit(conn, category, lat, lng):
        city = determine_city(lat, lng)
        unit_filter = "%"
        if category == "Fire":
            unit_filter = "%Fire Truck%"
        elif category == "Medical":
            unit_filter = "%Ambulance%"
        elif category == "Security":
            unit_filter = "%Police%"

        query = "SELECT * FROM fleet WHERE current_status = 'Standby' AND unit_type LIKE ?"
        params = [unit_filter]
        if city != "Unknown":
            query += " AND city = ?"
            params.append(city)

        choices = conn.execute(query, params).fetchall()

        if not choices:
            # If no specific category, try all in city
            query = "SELECT * FROM fleet WHERE current_status = 'Standby'"
            params = []
            if city != "Unknown":
                query += " AND city = ?"
                params.append(city)
            choices = conn.execute(query, params).fetchall()
        if not choices:
            return None

        nearest = min(
            choices,
            key=lambda row: calculate_distance(lat, lng, row["last_lat"], row["last_lng"])
        )
        return dict(nearest)

    async def handle_incident_update(llm, args):
        category = args.get("category", "General")
        severity = args.get("severity", "Medium")
        user_name = args.get("user_name", "Anonymous")
        location = args.get("location_details", "Unknown Location")

        if device_lat is None or device_lng is None:
            base_lat, base_lng = 9.9312, 76.2673
            import random
            device_lat = base_lat + random.uniform(-0.01, 0.01)
            device_lng = base_lng + random.uniform(-0.01, 0.01)

        conn = get_db()
        try:
            conn.execute('''
                UPDATE incidents
                SET category = ?, severity = ?, status = 'RESOURCE_ALLOCATED', user_name = ?,
                    location_details = ?, lat = ?, lng = ?, updated_at = ?
                WHERE id = ?
            ''', (
                category,
                severity,
                user_name,
                location,
                device_lat,
                device_lng,
                datetime.now().isoformat(),
                incident_id
            ))

            selected_unit = assign_nearest_unit(conn, category, device_lat, device_lng)
            if selected_unit:
                conn.execute(
                    "UPDATE fleet SET current_status = 'En Route', assigned_incident_id = ? WHERE unit_id = ?",
                    (incident_id, selected_unit["unit_id"])
                )
                conn.execute(
                    "UPDATE incidents SET assigned_unit = ?, resource_contact = ? WHERE id = ?",
                    (selected_unit["unit_id"], selected_unit["contact"], incident_id)
                )
                conn.commit()
                print(f"✅ Dispatched {selected_unit['unit_id']} for {category} near {device_lat}, {device_lng}")
                city = determine_city(device_lat, device_lng)
                return {
                    "status": "success",
                    "message": f"Assigned {selected_unit['unit_id']} from {city} fleet.",
                    "assigned_unit": selected_unit["unit_id"],
                    "resource_contact": selected_unit["contact"]
                }

            conn.commit()
            return {"status": "success", "message": "No standby fleet unit available yet."}
        except Exception as e:
            print(f"Database error during incident update: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            conn.close()

    llm.register_function("update_incident_details", handle_incident_update)

    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        db_processor,
        tts,
        transport.output(),
        context_aggregator.assistant()
    ])

    task = PipelineTask(pipeline, params=PipelineParams(
        allow_interruptions=True,
        enable_turn_tracking=True,
        enable_rtvi=False,
        enable_usage_metrics=False,
        prefix_messages=[]
    ))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        await task.queue_frames([
            LLMMessagesUpdateFrame(
                messages=[{"role": "system", "content": "The user is connected. Greet them as the GuardianSync Coordinator and ask how you can help."}],
                run_llm=True
            )
        ])

    runner = PipelineRunner()
    await runner.run(task)
