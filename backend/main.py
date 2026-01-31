"""FastAPI backend for LLM Council - WITH AUTHENTICATION."""

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import List, Dict, Any
import uuid
import json
import asyncio
import secrets
import os

from . import storage
from .council import run_full_council, generate_conversation_title, stage1_collect_responses, stage2_collect_rankings, stage3_synthesize_final, calculate_aggregate_rankings

# ============== AUTHENTICATION ==============
security = HTTPBasic()

AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD")  # No default = auth disabled if not set

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Verify HTTP Basic Auth credentials."""
    # If no password configured, skip auth (local development)
    if not AUTH_PASSWORD:
        return "anonymous"
    
    is_correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"), 
        AUTH_USERNAME.encode("utf8")
    )
    is_correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"), 
        AUTH_PASSWORD.encode("utf8")
    )
    
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# ============== APP SETUP ==============
app = FastAPI(title="LLM Council API")

# Enable CORS - update origins for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============== REQUEST MODELS ==============
class CreateConversationRequest(BaseModel):
    """Request to create a new conversation."""
    pass


class SendMessageRequest(BaseModel):
    """Request to send a message in a conversation."""
    content: str


# ============== API ROUTES (ALL PROTECTED) ==============

@app.post("/api/conversations")
async def create_conversation(
    request: CreateConversationRequest = None,
    user: str = Depends(verify_credentials)
):
    """Create a new conversation."""
    conversation_id = str(uuid.uuid4())
    conversation = {
        "id": conversation_id,
        "title": "New Conversation",
        "messages": []
    }
    storage.save_conversation(conversation_id, conversation)
    return conversation


@app.get("/api/conversations")
async def list_conversations(user: str = Depends(verify_credentials)):
    """List all conversations."""
    return storage.list_conversations()


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user: str = Depends(verify_credentials)
):
    """Get a specific conversation."""
    conversation = storage.load_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user: str = Depends(verify_credentials)
):
    """Delete a conversation."""
    if storage.delete_conversation(conversation_id):
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Conversation not found")


@app.post("/api/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    request: SendMessageRequest,
    user: str = Depends(verify_credentials)
):
    """Send a message and get council response (non-streaming)."""
    conversation = storage.load_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Run the full council process
    result = await run_full_council(request.content)
    
    # Add messages to conversation
    conversation["messages"].append({
        "role": "user",
        "content": request.content
    })
    conversation["messages"].append({
        "role": "assistant",
        "content": result
    })
    
    # Generate title if first message
    if len(conversation["messages"]) == 2:
        title = await generate_conversation_title(request.content)
        conversation["title"] = title
    
    storage.save_conversation(conversation_id, conversation)
    return result


@app.post("/api/conversations/{conversation_id}/messages/stream")
async def send_message_stream(
    conversation_id: str,
    request: SendMessageRequest,
    user: str = Depends(verify_credentials)
):
    """Send a message and stream the council response."""
    conversation = storage.load_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    async def generate():
        # Add user message
        conversation["messages"].append({
            "role": "user",
            "content": request.content
        })
        
        # Stage 1: Collect individual responses
        yield f"data: {json.dumps({'type': 'stage1_start'})}\n\n"
        stage1_results = await stage1_collect_responses(request.content)
        yield f"data: {json.dumps({'type': 'stage1_complete', 'data': stage1_results})}\n\n"
        
        # Stage 2: Peer review and ranking
        yield f"data: {json.dumps({'type': 'stage2_start'})}\n\n"
        stage2_results, label_to_model = await stage2_collect_rankings(request.content, stage1_results)
        aggregate_rankings = calculate_aggregate_rankings(stage2_results, label_to_model)
        yield f"data: {json.dumps({'type': 'stage2_complete', 'data': stage2_results, 'metadata': {'label_to_model': label_to_model, 'aggregate_rankings': aggregate_rankings}})}\n\n"
        
        # Stage 3: Chairman synthesis
        yield f"data: {json.dumps({'type': 'stage3_start'})}\n\n"
        stage3_result = await stage3_synthesize_final(request.content, stage1_results, stage2_results)
        yield f"data: {json.dumps({'type': 'stage3_complete', 'data': stage3_result})}\n\n"
        
        # Save assistant response
        conversation["messages"].append({
            "role": "assistant",
            "content": {
                "stage1": stage1_results,
                "stage2": stage2_results,
                "stage3": stage3_result,
                "metadata": {
                    "label_to_model": label_to_model,
                    "aggregate_rankings": aggregate_rankings
                }
            }
        })
        
        # Generate title if first exchange
        if len(conversation["messages"]) == 2:
            title = await generate_conversation_title(request.content)
            conversation["title"] = title
            yield f"data: {json.dumps({'type': 'title_update', 'title': title})}\n\n"
        
        storage.save_conversation(conversation_id, conversation)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    
    return StreamingResponse(generate(), media_type="text/event-stream")


# ============== FRONTEND SERVING ==============
# Serve the built React frontend

frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")

if os.path.exists(frontend_path):
    # Serve static assets (JS, CSS, images) without auth for browser loading
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_path, "assets")), name="assets")
    
    @app.get("/")
    async def serve_root(user: str = Depends(verify_credentials)):
        """Serve the main page (requires auth)."""
        return FileResponse(os.path.join(frontend_path, "index.html"))
    
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str, user: str = Depends(verify_credentials)):
        """Serve frontend routes (requires auth)."""
        # Check if it's a static file
        file_path = os.path.join(frontend_path, full_path)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return FileResponse(file_path)
        # Otherwise serve index.html for client-side routing
        return FileResponse(os.path.join(frontend_path, "index.html"))
