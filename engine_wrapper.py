import os
import chess.engine
import backoff
import subprocess


@backoff.on_exception(backoff.expo, BaseException, max_time=120)
def create_engine(config):
    cfg = config["engine"]
    engine_path = os.path.join(cfg["dir"], cfg["name"])
    engine_type = cfg.get("protocol")
    engine_options = cfg.get("engine_options")
    commands = [engine_path]
    if engine_options:
        for k, v in engine_options.items():
            commands.append("--{}={}".format(k, v))

    silence_stderr = cfg.get("silence_stderr", False)

    Engine = XBoardEngine if engine_type == "xboard" else UCIEngine
    options = remove_managed_options(cfg.get(engine_type + "_options", {}) or {})
    return Engine(commands, options, silence_stderr)


def remove_managed_options(config):
    def is_managed(key):
        return chess.engine.Option(key, None, None, None, None, None).is_managed()

    return {name: value for (name, value) in config.items() if not is_managed(name)}


def print_handler_stats(info, stats):
    for stat in stats:
        if stat in info:
            print("    {}: {}".format(stat, info[stat]))


def get_handler_stats(info, stats):
    stats_str = []
    for stat in stats:
        if stat in info:
            stats_str.append("{}: {}".format(stat, info[stat]))

    return stats_str


class EngineWrapper:
    def __init__(self, commands, options=None, silence_stderr=False):
        pass

    def set_time_control(self, game):
        pass

    def first_search(self, board, movetime):
        pass

    def search_with_ponder(self, board, wtime, btime, winc, binc, ponder=False):
        pass

    def ponderhit(self):
        pass

    def print_stats(self):
        pass

    def get_opponent_info(self, game):
        pass

    def name(self):
        return self.engine.id["name"]

    def stop(self):
        pass

    def quit(self):
        self.engine.quit()


class UCIEngine(EngineWrapper):
    def __init__(self, commands, options, silence_stderr=False):
        self.go_commands = options.pop("go_commands", {}) or {}
        self.engine = chess.engine.SimpleEngine.popen_uci(commands, stderr=subprocess.DEVNULL if silence_stderr else None)
        self.engine.configure(options)
        self.last_move_info = {}

    def first_search(self, board, movetime):
        result = self.engine.play(board, chess.engine.Limit(time=movetime / 1000), info=chess.engine.INFO_ALL)
        self.last_move_info = result.info
        return result.move

    def search_with_ponder(self, board, wtime, btime, winc, binc, ponder=False):
        cmds = self.go_commands
        movetime = cmds.get("movetime")
        if movetime is not None:
            movetime = float(movetime) / 1000
        time_limit = chess.engine.Limit(white_clock=wtime / 1000,
                                        black_clock=btime / 1000,
                                        white_inc=winc / 1000,
                                        black_inc=binc / 1000,
                                        depth=cmds.get("depth"),
                                        nodes=cmds.get("nodes"),
                                        time=movetime)
        result = self.engine.play(board, time_limit, ponder=ponder, info=chess.engine.INFO_ALL)
        self.last_move_info = result.info
        return (result.move, result.ponder)

    def stop(self):
        self.engine.protocol.send_line("stop")

    def print_stats(self):
        print_handler_stats(self.last_move_info, ["string", "depth", "nps", "nodes", "score"])

    def get_stats(self):
        return get_handler_stats(self.last_move_info, ["depth", "nps", "nodes", "score"])

    def get_opponent_info(self, game):
        name = game.opponent.name
        if name and "UCI_Opponent" in self.engine.protocol.config:
            rating = game.opponent.rating if game.opponent.rating is not None else "none"
            title = game.opponent.title if game.opponent.title else "none"
            player_type = "computer" if title == "BOT" else "human"
            self.engine.configure({"UCI_Opponent": f"{title} {rating} {player_type} {name}"})

    def ponderhit(self):
        self.engine.protocol.send_line("ponderhit")


class XBoardEngine(EngineWrapper):
    def __init__(self, commands, options=None, silence_stderr=False):
        self.engine = chess.engine.SimpleEngine.popen_xboard(commands, stderr=subprocess.DEVNULL if silence_stderr else None)

        egt_paths = options.pop("egtpath", {}) or {}
        features = self.engine.protocol.features
        egt_types_from_engine = features["egt"].split(",") if "egt" in features else []
        for egt_type in egt_types_from_engine:
            options[f"egtpath {egt_type}"] = egt_paths[egt_type]
        self.engine.configure(options)

        self.last_move_info = {}
        self.time_control_sent = False

    def set_time_control(self, game):
        self.minutes = game.clock_initial // 1000 // 60
        self.seconds = game.clock_initial // 1000 % 60
        self.inc = game.clock_increment // 1000

    def send_time(self):
        self.engine.protocol.send_line(f"level 0 {self.minutes}:{self.seconds} {self.inc}")
        self.time_control_sent = True

    def first_search(self, board, movetime):
        result = self.engine.play(board,
                                  chess.engine.Limit(time=movetime // 1000),
                                  info=chess.engine.INFO_ALL)
        self.last_move_info = result.info
        return result.move

    def search_with_ponder(self, board, wtime, btime, winc, binc, ponder=False):
        if not self.time_control_sent:
            self.send_time()

        time_limit = chess.engine.Limit(white_clock=wtime / 1000,
                                        black_clock=btime / 1000)
        result = self.engine.play(board,
                                  time_limit,
                                  info=chess.engine.INFO_ALL,
                                  ponder=ponder)
        self.last_move_info = result.info
        return result.move, None

    def stop(self):
        self.engine.protocol.send_line("?")

    def print_stats(self):
        print_handler_stats(self.last_move_info, ["depth", "nodes", "score"])

    def get_stats(self):
        return get_handler_stats(self.last_move_info, ["depth", "nodes", "score"])

    def get_opponent_info(self, game):
        if game.opponent.name and self.engine.protocol.features.get("name", True):
            title = game.opponent.title + " " if game.opponent.title else ""
            self.engine.protocol.send_line(f"name {title}{game.opponent.name}")
        if game.me.rating is not None and game.opponent.rating is not None:
            self.engine.protocol.send_line(f"rating {game.me.rating} {game.opponent.rating}")
        if game.opponent.title == "BOT":
            self.engine.protocol.send_line("computer")
