{% if part == 'system' %}
You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation.
{% elif part == 'user' %}
Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{{ current_memory }}

## Conversation to Process
{{ conversation }}
{% endif %}
