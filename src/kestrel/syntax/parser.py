from datetime import datetime, timedelta
from pkgutil import get_data

from firepit.query import BinnedColumn, Filter, Predicate
from firepit.timestamp import timefmt
from lark import Lark, Token, Transformer

from kestrel.utils import unescape_quoted_string


def parse(stmts, default_variable="_", default_sort_order="desc"):
    # the public parsing interface for Kestrel
    # return abstract syntax tree
    # check kestrel.lark for details
    grammar = get_data(__name__, "kestrel.lark").decode("utf-8")
    return Lark(
        grammar,
        parser="lalr",
        transformer=_PostParsing(default_variable, default_sort_order),
    ).parse(stmts)


def get_all_input_var_names(stmt):
    input_refs = ["input", "input_2", "variablesource"]
    inputs_refs = stmt["inputs"] if "inputs" in stmt else []
    return [stmt.get(k) for k in input_refs if k in stmt] + inputs_refs


################################################################
#                           Private
################################################################


class _PostParsing(Transformer):
    def __init__(self, default_variable, default_sort_order):
        self.default_variable = default_variable
        self.default_sort_order = default_sort_order
        super().__init__()

    def start(self, args):
        return args

    def statement(self, args):
        # Kestrel syntax: a statement can only has one command
        stmt = args.pop()
        return stmt

    def assignment(self, args):
        stmt = args[1] if len(args) == 2 else args[0]
        stmt["output"] = _extract_var(args, self.default_variable)
        return stmt

    def assign(self, args):
        packet = args[0]  # Already transformed in expression method below
        packet["command"] = "assign"
        return packet

    def merge(self, args):
        return {
            "command": "merge",
            "inputs": _extract_vars(args, self.default_variable),
        }

    def info(self, args):
        return {"command": "info", "input": _extract_var(args, self.default_variable)}

    def disp(self, args):
        packet = {"command": "disp"}
        for arg in args:
            if isinstance(arg, dict):
                packet.update(arg)
        if "attrs" not in packet:
            packet["attrs"] = "*"
        return packet

    def get(self, args):
        packet = {
            "command": "get",
            "type": _extract_entity_type(args),
            "patternbody": _assert_and_extract_single("STIXPATTERNBODY", args),
        }

        for item in args:
            if isinstance(item, dict):
                packet.update(item)

        if "timerange" not in packet:
            packet["timerange"] = None

        return packet

    def find(self, args):
        packet = {
            "command": "find",
            "type": _extract_entity_type(args),
            "relation": _assert_and_extract_single("RELATION", args).lower(),
            "reversed": _extract_if_reversed(args),
            "input": _extract_var(args, self.default_variable),
        }

        for item in args:
            if isinstance(item, dict):
                packet.update(item)

        if "timerange" not in packet:
            packet["timerange"] = None

        return packet

    def join(self, args):
        packet = {
            "command": "join",
            "input": _first(args),
            "input_2": _second(args),
        }
        if len(args) == 5:
            packet["attribute_1"] = _fourth(args)
            packet["attribute_2"] = _fifth(args)

        return packet

    def group(self, args):
        packet = {
            "command": "group",
            "attributes": args[2],
            "input": _extract_var(args, self.default_variable),
        }
        aggregations = args[3] if len(args) > 3 else None
        if aggregations:
            packet["aggregations"] = aggregations
        return packet

    def sort(self, args):
        return {
            "command": "sort",
            "attribute": _extract_attribute(args),
            "input": _extract_var(args, self.default_variable),
            "ascending": _extract_direction(args, self.default_sort_order),
        }

    def apply(self, args):
        packet = {"command": "apply", "arguments": {}}
        for arg in args:
            if isinstance(arg, dict):
                if "variables" in arg:
                    packet["inputs"] = arg["variables"]
                else:
                    packet.update(arg)
        return packet

    def load(self, args):
        packet = {
            "command": "load",
            "type": _extract_entity_type(args),
        }
        for arg in args:
            if isinstance(arg, dict):
                packet.update(arg)
        return packet

    def save(self, args):
        packet = {
            "command": "save",
            "input": _extract_var(args, self.default_variable),
        }
        for arg in args:
            if isinstance(arg, dict):
                packet.update(arg)
        return packet

    def new(self, args):
        return {
            "command": "new",
            "type": _extract_entity_type(args),
            "data": _assert_and_extract_single("VAR_DATA", args),
        }

    def expression(self, args):
        packet = args[0]
        for arg in args:
            packet.update(arg)
        return packet

    def transform(self, args):
        return {
            "input": _extract_var(args, self.default_variable),
            "transform": _assert_and_extract_single("TRANSFORM", args),
        }

    def where_clause(self, args):
        return {
            "where": Filter([args[0]]),
        }

    def attr_clause(self, args):
        paths = _assert_and_extract_single("ATTRIBUTES", args)
        return {
            "attrs": paths if paths else "*",
        }

    def sort_clause(self, args):
        return {
            "attribute": _extract_attribute(args),
            "ascending": _extract_direction(args, self.default_sort_order),
        }

    def limit_clause(self, args):
        return {
            "limit": int(_first(args)),
        }

    def offset_clause(self, args):
        return {
            "offset": int(_first(args)),
        }

    def timespan_relative(self, args):
        num = int(args[0])
        unit = args[1]
        if unit.type == "DAY":
            delta = timedelta(days=num)
        elif unit.type == "HOUR":
            delta = timedelta(hours=num)
        elif unit.type == "MINUTE":
            delta = timedelta(minutes=num)
        elif unit.type == "SECOND":
            delta = timedelta(seconds=num)
        stop = datetime.utcnow()
        start = stop - delta
        return {"timerange": (timefmt(start, prec=6), timefmt(stop, prec=6))}

    def timespan_absolute(self, args):
        return {"timerange": (args[0], args[1])}

    def timestamp(self, args):
        return _assert_and_extract_single("ISOTIMESTAMP", args)

    def entity_type(self, args):
        return _first(args)

    def variables(self, args):
        return {"variables": _extract_vars(args, self.default_variable)}

    def stdpath(self, args):
        v = _first(args)
        if args[0].type == "PATH_ESCAPED":
            v = unescape_quoted_string(v)
        return {"path": v}

    def datasource(self, args):
        v = _first(args)
        if args[0].type == "DATASRC_ESCAPED":
            v = unescape_quoted_string(v)
        return {"datasource": v}

    def analytics_uri(self, args):
        v = _first(args)
        if args[0].type == "ANALYTICS_ESCAPED":
            v = unescape_quoted_string(v)
        return {"analytics_uri": v}

    # automatically put one or more grp_expr into a list
    def grp_spec(self, args):
        return args

    def grp_expr(self, args):
        item = args[0]
        if isinstance(item, Token):
            # an ATTRIBUTE
            return str(item)
        else:
            # bin_func
            return item

    def bin_func(self, args):
        attr = _first(args)
        num = int(_second(args))
        if len(args) >= 3:
            unit = _third(args)
        else:
            unit = None
        alias = f"{attr}_bin"
        return BinnedColumn(attr, num, unit, alias=alias)

    def agg_list(self, args):
        return [arg for arg in args]

    def agg(self, args):
        func = _first(args).lower()
        attr = _second(args)
        alias = _third(args) if len(args) > 2 else f"{func}_{attr}"
        return {"func": func, "attr": attr, "alias": alias}

    def exp_or(self, args):
        lhs = args[0]
        rhs = args[1]
        return Predicate(lhs, "OR", rhs)

    def exp_and(self, args):
        lhs = args[0]
        rhs = args[1]
        return Predicate(lhs, "AND", rhs)

    def exp_comparison_std(self, args):
        lhs = _first(args)
        op = _second(args)
        rhs = args[2]
        return Predicate(lhs, op, rhs)

    def exp_comparison_null(self, args):
        lhs = _first(args)
        op = _second(args).upper()
        if "NOT" in op:
            op = "!="
        else:
            op = "="
        rhs = "NULL"
        return Predicate(lhs, op, rhs)

    def args(self, args):
        d = {}
        for di in args:
            d.update(di)
        return {"arguments": d}

    def arg_kv_pair(self, args):
        return {_first(args): args[1]}

    def arg_values(self, args):
        if len(args) == 1:
            return args[0]
        else:
            return args

    def value(self, args):
        if args[0].type in ("NUMBER", "SIGNED_NUMBER"):
            try:
                v = int(args[0].value)
            except:
                v = float(args[0].value)
        elif args[0].type == "ESCAPED_STRING":
            v = unescape_quoted_string(args[0].value)
        else:
            v = args[0].value
        return v


def _first(args):
    return args[0].value


def _second(args):
    return args[1].value


def _third(args):
    return args[2].value


def _fourth(args):
    return args[3].value


def _fifth(args):
    return args[4].value


def _last(args):
    return args[-1].value


def _assert_and_extract_single(arg_type, args):
    items = [arg.value for arg in args if hasattr(arg, "type") and arg.type == arg_type]
    assert len(items) <= 1
    return items.pop() if items else None


def _extract_var(args, default_variable):
    # extract a single variable from the args
    # default variable if no variable is found
    v = _assert_and_extract_single("VARIABLE", args)
    return v if v else default_variable


def _extract_vars(args, default_variable):
    var_names = []
    for arg in args:
        if hasattr(arg, "type") and arg.type == "VARIABLE":
            var_names.append(arg.value)
    if not var_names:
        var_names = [default_variable]
    return var_names


def _extract_stixpath(args):
    # extract a single stix path from the args
    return _assert_and_extract_single("STIXPATH", args)


def _extract_attribute(args):
    # extract a single attribute from the args
    return _assert_and_extract_single("ATTRIBUTE", args)


def _extract_datasource(args):
    raw_ds = _assert_and_extract_single("DATASRC", args)
    ds = raw_ds.strip('"') if raw_ds else None
    return ds


def _extract_entity_type(args):
    # extract a single entity type from the args
    return _assert_and_extract_single("ENTITY_TYPE", args)


def _extract_direction(args, default_sort_order):
    # extract sort direction from args
    # default direction if no variable is found
    # return: if descending
    ds = [
        x for x in args if hasattr(x, "type") and (x.type == "ASC" or x.type == "DESC")
    ]
    assert len(ds) <= 1
    d = ds.pop().type if ds else default_sort_order
    return True if d == "ASC" else False


def _extract_if_reversed(args):
    rs = [x for x in args if hasattr(x, "type") and x.type == "REVERSED"]
    return True if rs else False
