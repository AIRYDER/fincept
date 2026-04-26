class FinceptError(Exception):
    pass


class ContractError(FinceptError):
    pass


class ConfigError(FinceptError):
    pass


class ConnectionError(FinceptError):
    pass


class RiskError(FinceptError):
    pass


class KillSwitchActive(FinceptError):
    pass
