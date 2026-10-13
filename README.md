# Building a Custom Voice Agent with LiveKit (Open Source)

This repository contains the source code for an AI voice agent built using LiveKit, an open-source framework and developer platform for real-time voice, video, and physical AI.

This project demonstrates how to build an LLM-based application that listens, thinks, and responds with natural-sounding speech in real time using a complete, high-performance STT-LLM-TTS (Speech-to-Text, Large Language Model, Text-to-Speech) pipeline.

## Overview

Specifically, this custom agent is designed for **tech support** and is configured to:
- Act as an upbeat, slightly sarcastic assistant.
- Handle real-world conversational dynamics automatically using built-in background voice cancellation, end-of-utterance turn detection, and seamless "barge-in" interruption handling.

## Key Features

- **Multi-Model Fallbacks:** Utilizes primary models with reliable backups to ensure high availability (e.g., OpenAI and Cartesia as primary, with Gemini and Inworld as backups).
- **Tool Calling (Function Calling):** 
  - Look up the current weather using the Open-Meteo API.
  - Fetch user account details from a custom endpoint using an email address.
- **Open Source Control:** Because LiveKit is fully open source (under the Apache 2.0 license), you have full control over the agent's behavior and can run the entire stack on your own servers. 

## Architecture

The codebase is structured so that the LiveKit server orchestrates the real-time WebRTC media transport and low-latency communication, while this Python script coordinates the logic and routing between the different AI model providers.

## Prerequisites
- Python 3.9 or higher
- A LiveKit Cloud project (or local LiveKit server)

## Setup

1. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
   ```

2. **Install the dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up your environment variables:**
   Create a `.env` file in the root directory and add your API keys:
   ```env
   LIVEKIT_URL=your_livekit_url
   LIVEKIT_API_KEY=your_livekit_api_key
   LIVEKIT_API_SECRET=your_livekit_api_secret
   
   # Provider Keys
   OPENAI_API_KEY=your_openai_api_key
   GEMINI_API_KEY=your