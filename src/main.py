# Standard Packages
import sys
import json
from typing import Optional

# External Packages
import uvicorn
from fastapi import FastAPI

# Internal Packages
from src.search_type import asymmetric, symmetric_ledger, image_search
from src.utils.helpers import get_absolute_path, get_from_dict
from src.utils.cli import cli
from src.utils.config import SearchType, SearchModels, TextSearchConfig, ImageSearchConfig, SearchConfig, ProcessorConfig, ConversationProcessorConfig
from src.processor.conversation.gpt import converse, message_to_log, message_to_prompt, understand, summarize


# Application Global State
model = SearchModels()
search_config = SearchConfig()
processor_config = ProcessorConfig()
app = FastAPI()


@app.get('/search')
def search(q: str, n: Optional[int] = 5, t: Optional[SearchType] = None):
    if q is None or q == '':
        print(f'No query param (q) passed in API call to initiate search')
        return {}

    user_query = q
    results_count = n

    if (t == SearchType.Notes or t == None) and model.notes_search:
        # query notes
        hits = asymmetric.query(user_query, model.notes_search)

        # collate and return results
        return asymmetric.collate_results(hits, model.notes_search.entries, results_count)

    if (t == SearchType.Music or t == None) and model.music_search:
        # query music library
        hits = asymmetric.query(user_query, model.music_search)

        # collate and return results
        return asymmetric.collate_results(hits, model.music_search.entries, results_count)

    if (t == SearchType.Ledger or t == None) and model.ledger_search:
        # query transactions
        hits = symmetric_ledger.query(user_query, model.ledger_search)

        # collate and return results
        return symmetric_ledger.collate_results(hits, model.ledger_search.entries, results_count)

    if (t == SearchType.Image or t == None) and model.image_search:
        # query transactions
        hits = image_search.query(user_query, results_count, model.image_search)

        # collate and return results
        return image_search.collate_results(
            hits,
            model.image_search.image_names,
            search_config.image.input_directory,
            results_count)

    else:
        return {}


@app.get('/regenerate')
def regenerate(t: Optional[SearchType] = None):
    if (t == SearchType.Notes or t == None) and search_config.notes:
        # Extract Entries, Generate Embeddings
        model.notes_search = asymmetric.setup(search_config.notes, regenerate=True)

    if (t == SearchType.Music or t == None) and search_config.music:
        # Extract Entries, Generate Song Embeddings
        model.music_search = asymmetric.setup(search_config.music, regenerate=True)

    if (t == SearchType.Ledger or t == None) and search_config.ledger:
        # Extract Entries, Generate Embeddings
        model.ledger_search = symmetric_ledger.setup(search_config.ledger, regenerate=True)

    if (t == SearchType.Image or t == None) and search_config.image:
        # Extract Images, Generate Embeddings
        model.image_search = image_search.setup(search_config.image, regenerate=True)

    return {'status': 'ok', 'message': 'regeneration completed'}


@app.get('/chat')
def chat(q: str):
    # Load Conversation History
    chat_session = processor_config.conversation.chat_session
    meta_log = processor_config.conversation.meta_log

    # Converse with OpenAI GPT
    metadata = understand(q, api_key=processor_config.conversation.openai_api_key)
    if get_from_dict(metadata, "intent", "memory-type") == "notes":
        query = get_from_dict(metadata, "intent", "query")
        result_list = search(query, n=1, t=SearchType.Notes)
        collated_result = "\n".join([item["Entry"] for item in result_list])
        gpt_response = summarize(collated_result, summary_type="notes", user_query=q, api_key=processor_config.conversation.openai_api_key)
    else:
        gpt_response = converse(q, chat_session, api_key=processor_config.conversation.openai_api_key)

    # Update Conversation History
    processor_config.conversation.chat_session = message_to_prompt(q, chat_session, gpt_message=gpt_response)
    processor_config.conversation.meta_log['chat'] = message_to_log(q, metadata, gpt_response, meta_log.get('chat', []))

    return {'status': 'ok', 'response': gpt_response}


def initialize_search(config, regenerate, verbose):
    model = SearchModels()
    search_config = SearchConfig()

    # Initialize Org Notes Search
    search_config.notes = TextSearchConfig.create_from_dictionary(config, ('content-type', 'org'), verbose)
    if search_config.notes:
        model.notes_search = asymmetric.setup(search_config.notes, regenerate=regenerate)

    # Initialize Org Music Search
    search_config.music = TextSearchConfig.create_from_dictionary(config, ('content-type', 'music'), verbose)
    if search_config.music:
        model.music_search = asymmetric.setup(search_config.music, regenerate=regenerate)

    # Initialize Ledger Search
    search_config.ledger = TextSearchConfig.create_from_dictionary(config, ('content-type', 'ledger'), verbose)
    if search_config.ledger:
        model.ledger_search = symmetric_ledger.setup(search_config.ledger, regenerate=regenerate)

    # Initialize Image Search
    search_config.image = ImageSearchConfig.create_from_dictionary(config, ('content-type', 'image'), verbose)
    if search_config.image:
        model.image_search = image_search.setup(search_config.image, regenerate=regenerate)

    return model, search_config


def initialize_processor(config, verbose):
    # Initialize Conversation Processor
    processor_config = ProcessorConfig()
    processor_config.conversation = ConversationProcessorConfig.create_from_dictionary(config, ('processor', 'conversation'), verbose)

    conversation_logfile = processor_config.conversation.conversation_logfile
    if processor_config.conversation.verbose:
        print('INFO:\tLoading conversation logs from disk...')

    if conversation_logfile.expanduser().absolute().is_file():
        # Load Metadata Logs from Conversation Logfile
        with open(get_absolute_path(conversation_logfile), 'r') as f:
            processor_config.conversation.meta_log = json.load(f)

        print('INFO:\tConversation logs loaded from disk.')
    else:
        # Initialize Conversation Logs
        processor_config.conversation.meta_log = {}
        processor_config.conversation.chat_session = ""

    return processor_config


@app.on_event('shutdown')
def shutdown_event():
    # No need to create empty log file
    if not processor_config.conversation.meta_log:
        return
    elif processor_config.conversation.verbose:
        print('INFO:\tSaving conversation logs to disk...')

    # Summarize Conversation Logs for this Session
    chat_session = processor_config.conversation.chat_session
    openai_api_key = processor_config.conversation.openai_api_key
    conversation_log = processor_config.conversation.meta_log
    session = {
        "summary": summarize(chat_session, summary_type="chat", api_key=openai_api_key),
        "session-start": conversation_log.get("session", [{"session-end": 0}])[-1]["session-end"],
        "session-end": len(conversation_log["chat"])
        }
    if 'session' in conversation_log:
        conversation_log['session'].append(session)
    else:
        conversation_log['session'] = [session]

    # Save Conversation Metadata Logs to Disk
    conversation_logfile = get_absolute_path(processor_config.conversation.conversation_logfile)
    with open(conversation_logfile, "w+", encoding='utf-8') as logfile:
        json.dump(conversation_log, logfile)

    print('INFO:\tConversation logs saved to disk.')


if __name__ == '__main__':
    # Load config from CLI
    args = cli(sys.argv[1:])

    # Initialize Search from Config
    model, search_config = initialize_search(args.config, args.regenerate, args.verbose)

    # Initialize Processor from Config
    processor_config = initialize_processor(args.config, args.verbose)

    # Start Application Server
    if args.socket:
        uvicorn.run(app, proxy_headers=True, uds=args.socket)
    else:
        uvicorn.run(app, host=args.host, port=args.port)
