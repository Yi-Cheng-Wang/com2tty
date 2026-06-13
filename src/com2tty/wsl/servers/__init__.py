"""Loopback TCP servers the bridge runs inside WSL.

Both servers exist so that unmodified upload tools can talk to the bridged
device: the RFC 2217 forwarder accepts esptool/bossac/avrdude connections,
and the UF2 relay accepts firmware images from the intercepted picotool
wrapper. Each relays its traffic over the bridge's stdin/stdout pipes,
coordinating exclusive ownership of those pipes through shared events.
"""
