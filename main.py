import asyncio
from absl import app, flags
import logging
import glob
import os
import sys
import pickle

import questionary

from rich.console import Console

console = Console()

import interface  # for printing to terminal
import memgpt.agent as agent
import memgpt.system as system
import memgpt.utils as utils
import memgpt.presets as presets
import memgpt.constants as constants
import memgpt.personas.personas as personas
import memgpt.humans.humans as humans
from memgpt.persistence_manager import (
    InMemoryStateManager,
    InMemoryStateManagerWithPreloadedArchivalMemory,
    InMemoryStateManagerWithFaiss,
)

from config import Config

FLAGS = flags.FLAGS
flags.DEFINE_string("persona", default=None, required=False, help="Specify persona")
flags.DEFINE_string("human", default=None, required=False, help="Specify human")
flags.DEFINE_string(
    "model",
    default=constants.DEFAULT_MEMGPT_MODEL,
    required=False,
    help="Specify the LLM model",
)
flags.DEFINE_boolean(
    "first",
    default=False,
    required=False,
    help="Use -first to send the first message in the sequence",
)
flags.DEFINE_boolean(
    "debug", default=False, required=False, help="Use -debug to enable debugging output"
)
flags.DEFINE_boolean(
    "no_verify", default=False, required=False, help="Bypass message verification"
)
flags.DEFINE_string(
    "archival_storage_faiss_path",
    default="",
    required=False,
    help="Specify archival storage with FAISS index to load (a folder with a .index and .json describing documents to be loaded)",
)
flags.DEFINE_string(
    "archival_storage_files",
    default="",
    required=False,
    help="Specify files to pre-load into archival memory (glob pattern)",
)
flags.DEFINE_string(
    "archival_storage_files_compute_embeddings",
    default="",
    required=False,
    help="Specify files to pre-load into archival memory (glob pattern), and compute embeddings over them",
)
flags.DEFINE_string(
    "archival_storage_sqldb",
    default="",
    required=False,
    help="Specify SQL database to pre-load into archival memory",
)
# Support for Azure OpenAI (see: https://github.com/openai/openai-python#microsoft-azure-endpoints)
flags.DEFINE_boolean(
    "use_azure_openai",
    default=False,
    required=False,
    help="Use Azure OpenAI (requires additional environment variables)",
)


def clear_line():
    if os.name == "nt":  # for windows
        console.print("\033[A\033[K", end="")
    else:  # for linux
        sys.stdout.write("\033[2K\033[G")
        sys.stdout.flush()


def save(memgpt_agent, cfg):
    filename = utils.get_local_time().replace(" ", "_").replace(":", "_")
    filename = f"{filename}.json"
    filename = os.path.join("saved_state", filename)
    try:
        if not os.path.exists("saved_state"):
            os.makedirs("saved_state")
        memgpt_agent.save_to_json_file(filename)
        print(f"Saved checkpoint to: {filename}")
        cfg.agent_save_file = filename
    except Exception as e:
        print(f"Saving state to {filename} failed with: {e}")

    # save the persistence manager too
    filename = filename.replace(".json", ".persistence.pickle")
    try:
        memgpt_agent.persistence_manager.save(filename)
        print(f"Saved persistence manager to: {filename}")
        cfg.persistence_manager_save_file = filename
    except Exception as e:
        print(f"Saving persistence manager to {filename} failed with: {e}")
    cfg.write_config()


def load(memgpt_agent, filename):
    if filename is not None:
        if filename[-5:] != ".json":
            filename += ".json"
        try:
            memgpt_agent.load_from_json_file_inplace(filename)
            print(f"Loaded checkpoint {filename}")
        except Exception as e:
            print(f"Loading {filename} failed with: {e}")
    else:
        # Load the latest file
        print(
            f"/load warning: no checkpoint specified, loading most recent checkpoint instead"
        )
        json_files = glob.glob(
            "saved_state/*.json"
        )  # This will list all .json files in the current directory.

        # Check if there are any json files.
        if not json_files:
            print(f"/load error: no .json checkpoint files found")
        else:
            # Sort files based on modified timestamp, with the latest file being the first.
            filename = max(json_files, key=os.path.getmtime)
            try:
                memgpt_agent.load_from_json_file_inplace(filename)
                print(f"Loaded checkpoint {filename}")
            except Exception as e:
                print(f"Loading {filename} failed with: {e}")

    # need to load persistence manager too
    filename = filename.replace(".json", ".persistence.pickle")
    try:
        memgpt_agent.persistence_manager = InMemoryStateManager.load(
            filename
        )  # TODO(fixme):for different types of persistence managers that require different load/save methods
        print(f"Loaded persistence manager from {filename}")
    except Exception as e:
        print(
            f"/load warning: loading persistence manager from {filename} failed with: {e}"
        )


async def main():
    utils.DEBUG = FLAGS.debug
    logging.getLogger().setLevel(logging.CRITICAL)
    if FLAGS.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if any(
        (
            FLAGS.persona,
            FLAGS.human,
            FLAGS.model != constants.DEFAULT_MEMGPT_MODEL,
            FLAGS.archival_storage_faiss_path,
            FLAGS.archival_storage_files,
            FLAGS.archival_storage_files_compute_embeddings,
            FLAGS.archival_storage_sqldb,
        )
    ):
        interface.important_message("⚙️ Using legacy command line arguments.")
        model = FLAGS.model
        if model is None:
            model = constants.DEFAULT_MEMGPT_MODEL
        memgpt_persona = FLAGS.persona
        if memgpt_persona is None:
            memgpt_persona = (
                personas.GPT35_DEFAULT if "gpt-3.5" in model else personas.DEFAULT
            )
        human_persona = FLAGS.human
        if human_persona is None:
            human_persona = humans.DEFAULT

        if FLAGS.archival_storage_files:
            cfg = await Config.legacy_flags_init(
                model,
                memgpt_persona,
                human_persona,
                load_type="folder",
                archival_storage_files=FLAGS.archival_storage_files,
                compute_embeddings=False,
            )
        elif FLAGS.archival_storage_faiss_path:
            cfg = await Config.legacy_flags_init(
                model,
                memgpt_persona,
                human_persona,
                load_type="folder",
                archival_storage_index=FLAGS.archival_storage_index,
                compute_embeddings=False,
            )
        elif FLAGS.archival_storage_files_compute_embeddings:
            print(model)
            print(memgpt_persona)
            print(human_persona)
            cfg = await Config.legacy_flags_init(
                model,
                memgpt_persona,
                human_persona,
                load_type="folder",
                archival_storage_files=FLAGS.archival_storage_files_compute_embeddings,
                compute_embeddings=True,
            )
        elif FLAGS.archival_storage_sqldb:
            cfg = await Config.legacy_flags_init(
                model,
                memgpt_persona,
                human_persona,
                load_type="sql",
                archival_storage_files=FLAGS.archival_storage_sqldb,
                compute_embeddings=False,
            )
        else:
            cfg = await Config.legacy_flags_init(
                model,
                memgpt_persona,
                human_persona,
            )
    else:
        cfg = await Config.config_init()

    interface.important_message("Running... [exit by typing '/exit']")
    if cfg.model != constants.DEFAULT_MEMGPT_MODEL:
        interface.warning_message(
            f"⛔️ Warning - you are running MemGPT with {cfg.model}, which is not officially supported (yet). Expect bugs!"
        )

    # Azure OpenAI support
    if FLAGS.use_azure_openai:
        azure_openai_key = os.getenv("AZURE_OPENAI_KEY")
        azure_openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        azure_openai_version = os.getenv("AZURE_OPENAI_VERSION")
        azure_openai_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        if None in [
            azure_openai_key,
            azure_openai_endpoint,
            azure_openai_version,
            azure_openai_deployment,
        ]:
            print(
                f"Error: missing Azure OpenAI environment variables. Please see README section on Azure."
            )
            return

        import openai

        openai.api_type = "azure"
        openai.api_key = azure_openai_key
        openai.api_base = azure_openai_endpoint
        openai.api_version = azure_openai_version
        # deployment gets passed into chatcompletion
    else:
        azure_openai_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        if azure_openai_deployment is not None:
            print(
                f"Error: AZURE_OPENAI_DEPLOYMENT should not be set if --use_azure_openai is False"
            )
            return

    if cfg.archival_storage_index:
        persistence_manager = InMemoryStateManagerWithFaiss(
            cfg.index, cfg.archival_database
        )
    elif cfg.archival_storage_files:
        print(f"Preloaded {len(cfg.archival_database)} chunks into archival memory.")
        persistence_manager = InMemoryStateManagerWithPreloadedArchivalMemory(
            cfg.archival_database
        )
    else:
        persistence_manager = InMemoryStateManager()

    if FLAGS.archival_storage_files_compute_embeddings:
        interface.important_message(
            f"(legacy) To avoid computing embeddings next time, replace --archival_storage_files_compute_embeddings={FLAGS.archival_storage_files_compute_embeddings} with\n\t --archival_storage_faiss_path={cfg.archival_storage_index} (if your files haven't changed)."
        )

    # Moved defaults out of FLAGS so that we can dynamically select the default persona based on model
    chosen_human = cfg.human_persona
    chosen_persona = cfg.memgpt_persona

    memgpt_agent = presets.use_preset(
        presets.DEFAULT,
        cfg.model,
        personas.get_persona_text(chosen_persona),
        humans.get_human_text(chosen_human),
        interface,
        persistence_manager,
    )
    print_messages = interface.print_messages
    await print_messages(memgpt_agent.messages)

    counter = 0
    user_input = None
    skip_next_user_input = False
    user_message = None
    USER_GOES_FIRST = FLAGS.first

    if cfg.load_type == "sql":  # TODO: move this into config.py in a clean manner
        if not os.path.exists(cfg.archival_storage_files):
            print(f"File {cfg.archival_storage_files} does not exist")
            return
        # Ingest data from file into archival storage
        else:
            print(f"Database found! Loading database into archival memory")
            data_list = utils.read_database_as_list(cfg.archival_storage_files)
            user_message = f"Your archival memory has been loaded with a SQL database called {data_list[0]}, which contains schema {data_list[1]}. Remember to refer to this first while answering any user questions!"
            for row in data_list:
                await memgpt_agent.persistence_manager.archival_memory.insert(row)
            print(f"Database loaded into archival memory.")

    if cfg.agent_save_file:
        load_save_file = await questionary.confirm(
            f"Load in saved agent '{cfg.agent_save_file}'?"
        ).ask_async()
        if load_save_file:
            load(memgpt_agent, cfg.agent_save_file)

    # auto-exit for
    if "GITHUB_ACTIONS" in os.environ:
        return

    if not USER_GOES_FIRST:
        console.input(
            "[bold cyan]Hit enter to begin (will request first MemGPT message)[/bold cyan]"
        )
        clear_line()
        print()

    while True:
        if not skip_next_user_input and (counter > 0 or USER_GOES_FIRST):
            # Ask for user input
            # user_input = console.input("[bold cyan]Enter your message:[/bold cyan] ")
            user_input = await questionary.text(
                "Enter your message:",
                multiline=True,
                qmark=">",
            ).ask_async()
            clear_line()

            if user_input.startswith("!"):
                print(f"Commands for CLI begin with '/' not '!'")
                continue

            if user_input == "":
                # no empty messages allowed
                print("Empty input received. Try again!")
                continue

            # Handle CLI commands
            # Commands to not get passed as input to MemGPT
            if user_input.startswith("/"):
                if user_input.lower() == "/exit":
                    # autosave
                    save(memgpt_agent=memgpt_agent, cfg=cfg)
                    break

                elif user_input.lower() == "/savechat":
                    filename = (
                        utils.get_local_time().replace(" ", "_").replace(":", "_")
                    )
                    filename = f"{filename}.pkl"
                    try:
                        if not os.path.exists("saved_chats"):
                            os.makedirs("saved_chats")
                        with open(os.path.join("saved_chats", filename), "wb") as f:
                            pickle.dump(memgpt_agent.messages, f)
                            print(f"Saved messages to: {filename}")
                    except Exception as e:
                        print(f"Saving chat to {filename} failed with: {e}")
                    continue

                elif user_input.lower() == "/save":
                    save(memgpt_agent=memgpt_agent, cfg=cfg)
                    continue

                elif user_input.lower() == "/load" or user_input.lower().startswith(
                    "/load "
                ):
                    command = user_input.strip().split()
                    filename = command[1] if len(command) > 1 else None
                    load(memgpt_agent=memgpt_agent, filename=filename)
                    continue

                elif user_input.lower() == "/dump":
                    await print_messages(memgpt_agent.messages)
                    continue

                elif user_input.lower() == "/dumpraw":
                    await interface.print_messages_raw(memgpt_agent.messages)
                    continue

                elif user_input.lower() == "/dump1":
                    await print_messages(memgpt_agent.messages[-1])
                    continue

                elif user_input.lower() == "/memory":
                    print(f"\nDumping memory contents:\n")
                    print(f"{str(memgpt_agent.memory)}")
                    print(f"{str(memgpt_agent.persistence_manager.archival_memory)}")
                    print(f"{str(memgpt_agent.persistence_manager.recall_memory)}")
                    continue

                elif user_input.lower() == "/model":
                    if memgpt_agent.model == "gpt-4":
                        memgpt_agent.model = "gpt-3.5-turbo"
                    elif memgpt_agent.model == "gpt-3.5-turbo":
                        memgpt_agent.model = "gpt-4"
                    print(f"Updated model to:\n{str(memgpt_agent.model)}")
                    continue

                elif user_input.lower() == "/pop" or user_input.lower().startswith(
                    "/pop "
                ):
                    # Check if there's an additional argument that's an integer
                    command = user_input.strip().split()
                    amount = (
                        int(command[1])
                        if len(command) > 1 and command[1].isdigit()
                        else 2
                    )
                    print(f"Popping last {amount} messages from stack")
                    for _ in range(min(amount, len(memgpt_agent.messages))):
                        memgpt_agent.messages.pop()
                    continue

                # No skip options
                elif user_input.lower() == "/wipe":
                    memgpt_agent = agent.AgentAsync(interface)
                    user_message = None

                elif user_input.lower() == "/heartbeat":
                    user_message = system.get_heartbeat()

                elif user_input.lower() == "/memorywarning":
                    user_message = system.get_token_limit_warning()

                else:
                    print(f"Unrecognized command: {user_input}")
                    continue

            else:
                # If message did not begin with command prefix, pass inputs to MemGPT
                # Handle user message and append to messages
                user_message = system.package_user_message(user_input)

        skip_next_user_input = False

        with console.status("[bold cyan]Thinking...") as status:
            (
                new_messages,
                heartbeat_request,
                function_failed,
                token_warning,
            ) = await memgpt_agent.step(
                user_message, first_message=False, skip_verify=FLAGS.no_verify
            )

            # Skip user inputs if there's a memory warning, function execution failed, or the agent asked for control
            if token_warning:
                user_message = system.get_token_limit_warning()
                skip_next_user_input = True
            elif function_failed:
                user_message = system.get_heartbeat(
                    constants.FUNC_FAILED_HEARTBEAT_MESSAGE
                )
                skip_next_user_input = True
            elif heartbeat_request:
                user_message = system.get_heartbeat(constants.REQ_HEARTBEAT_MESSAGE)
                skip_next_user_input = True

        counter += 1

    print("Finished.")


if __name__ == "__main__":

    def run(argv):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())

    app.run(run)
