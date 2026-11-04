"""Runnable preparation example. Does not send an email or access an app."""
import asyncio
import os
from personal_context import PersonalContext, connect

async def main():
    async with connect() as mem:
        memory = PersonalContext(mem, os.environ.get("MEMSEEK_OWNER", "owner"))
        # Prefer your trusted tool/recipient metadata over inferring it from the user text.
        frame = {"task": "Draft a work meeting cancellation email to client contact:sarah.",
                 "actions": ["cancel_meeting", "communicate"], "channels": ["email"],
                 "audiences": ["client"], "recipients": ["contact:sarah"], "domains": ["work"]}
        context = await memory.prepare(frame=frame, outward=True)
        print(context.content)
        print("Audit use:", context.id)
        # Replace the previous context block in the draft model's prompt with this one.
        # Use a fresh composer input: do not include private planner history/tool output.
        # draft = await YOUR_MODEL.draft(task=frame['task'], context=context.content)
        # YOUR_EXISTING_HARNESS validates the draft, approvals and send permissions.
        # Sending remains in your app connector; this package never sends anything.

if __name__ == "__main__":
    asyncio.run(main())
