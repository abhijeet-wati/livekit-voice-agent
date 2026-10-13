import logging
import time
import httpx
from dotenv import load_dotenv

from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AgentStateChangedEvent,
    AgentTask,
    JobContext,
    MetricsCollectedEvent,
    RunContext,
    ToolError,
    function_tool,
    inference,
    llm,
    metrics,
    room_io,
    stt,
    tts,
)
from livekit.plugins import noise_cancellation, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger(__name__)
load_dotenv()


# Define your agent's behavior by extending the Agent class
class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are an upbeat, slightly sarcastic voice AI for tech support."
                "Help the caller fix issues without rambling, and keep replies under 3 sentences. "
                "You can look up the weather if asked, and you can fetch user account details "
                "if the caller provides their email address."
            ),
        )

    # The @function_tool decorator registers this method as a tool the LLM can call
    @function_tool()
    async def lookup_weather(
        self,
        context: RunContext,  # Gives access to the session, speech handle, and user data
        location: str,  # Type hints help the LLM understand what arguments to pass
    ) -> dict:
        """Look up current weather for a location.
        
        Args:
            location: City name or location to get weather for.
        """
        async with httpx.AsyncClient() as client:
            # First, geocode the location to get coordinates
            geo_response = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1}
            )
            geo_data = geo_response.json()
            
            if not geo_data.get("results"):
                raise ToolError(f"Could not find location: {location}")
            
            lat = geo_data["results"][0]["latitude"]
            lon = geo_data["results"][0]["longitude"]
            place_name = geo_data["results"][0]["name"]
            
            # Get current weather for those coordinates
            weather_response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,weather_code",
                    "temperature_unit": "fahrenheit"
                }
            )
            weather = weather_response.json()
            
            # Return a dict with the weather data
            return {
                "location": place_name,
                "temperature_f": weather["current"]["temperature_2m"],
                "conditions": weather["current"]["weather_code"]
            }

    @function_tool()
    async def lookup_user_by_email(
        self,
        context: RunContext,
        email: str,
    ) -> dict:
        """Fetch user account details using their email address.
        
        Args:
            email: The email address of the user to look up.
        """
        api_url = f"https://e779-223-185-61-160.ngrok-free.app/getUserByEmail/{email}"
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(api_url)
                
                # Handle 404 or other HTTP errors gracefully
                if response.status_code == 404:
                    raise ToolError(f"User not found for email: {email}")
                
                response.raise_for_status()
                user_data = response.json()
                
                # Ensure the API didn't return an empty response
                if not user_data:
                    raise ToolError(f"No data returned for email: {email}")
                    
                return user_data
                
            except httpx.RequestError as e:
                raise ToolError(f"Failed to connect to the user database: {str(e)}")
        
    


server = AgentServer()


# The entrypoint function runs when a participant joins the room
@server.rtc_session()
async def entrypoint(ctx: JobContext):
    # Aggregate data across all conversation turns
    usage_collector = metrics.UsageCollector()

    # Track End of Utterance timing (when turn detector decides user finished speaking)
    last_eou_metrics: metrics.EOUMetrics | None = None

    # Configure the voice pipeline with STT, LLM, TTS, and VAD providers FIRST
    session = AgentSession(
        # LLM with fallback: OpenAI primary, Gemini backup
        llm=llm.FallbackAdapter(
            [
                inference.LLM(model="openai/gpt-4.1-mini"),
                inference.LLM(model="google/gemini-2.5-flash"),
            ]
        ),
        # STT with fallback: AssemblyAI primary, Deepgram backup
        stt=stt.FallbackAdapter(
            [
                inference.STT.from_model_string("assemblyai/universal-streaming:en"),
                inference.STT.from_model_string("deepgram/nova-3"),
            ]
        ),
        # TTS with fallback: Cartesia primary, Inworld backup
        tts=tts.FallbackAdapter(
            [
                inference.TTS.from_model_string("cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"),
                inference.TTS.from_model_string("inworld/inworld-tts-1"),
            ]
        ),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
        preemptive_generation=True,
    )

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        nonlocal last_eou_metrics
        # Capture EOU metrics for TTFA calculation
        if ev.metrics.type == "eou_metrics":
            last_eou_metrics = ev.metrics

        # Log each metric as it arrives and add to usage collector
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)
    
    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev: AgentStateChangedEvent):
        if ev.new_state == "speaking":
            if last_eou_metrics:
                # Calculate time since user finished speaking
                elapsed = time.time() - last_eou_metrics.timestamp
                logger.info(f"Time to first audio: {elapsed:.3f}s")

    async def log_usage():
        # Print per-session summary (tokens, audio duration, costs)
        summary = usage_collector.get_summary()
        logger.info("Usage summary: %s", summary)

    # Fire log_usage when worker shuts down
    ctx.add_shutdown_callback(log_usage)

    # Start the session with noise cancellation enabled
    await session.start(
        agent=Assistant(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVC(),  # Background voice cancellation
            ),
        ),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agents.cli.run_app(server)