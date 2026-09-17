"""CAN-flavored 29-bit header packing.

This is the canonical wire-header layout for the framework:

    [priority: 5][address: 8][pid: 11][type: 5]

Type field (5 bits): [is_write:1][is_multi:1][counter:3]
    is_write   1 = directed WRITE, 0 = pubsub BROADCAST
    is_multi   1 = part of a fragmented multi-frame message
    counter    mod-8 sequence counter (only meaningful if is_multi)

Used by CANBus (native), SerialTTLBus (CAN over USB serial), and as
the default header carried by floe's NullBus when no transport is
bound. Buses with fundamentally different addressing (e.g. MQTT topic
strings) define their own header classes.
"""

__all__ = ('CanHeader',)


class CanHeader:
    def __init__(self, *,
                 adr: int,
                 s: dict,
                 header_bits: int=29,
                 ad_bits: int=8,
                 priority_bits: int=5,
                 packet_size=8,
                 type_bits: int=5,
                 **k):
        """
        adr:0 -> EMCY
        adr:1 -> NETWORK
        adr:2 -> ZORG
        """
        self.s = s  # subscription list

        self.adr = adr  # this board's address
        self.header_bits = header_bits  # total # of bits
        self.ad_bits = ad_bits  # bits in address field
        self.num_adr = 2 ** ad_bits - 1
        self.priority_bits = priority_bits  # number of bits above address bits
        self.ad_mask = 2 ** self.ad_bits - 1
        self.type_bits = type_bits

        # constants for unpacking
        self.num_low = self.header_bits - self.ad_bits - self.priority_bits
        self.low_mask = 2 ** self.num_low - 1
        self.high_mask = (2 ** self.priority_bits - 1) << (self.num_low + self.ad_bits)
        self.type_mask = 2 ** self.type_bits - 1

        self.packet_size = packet_size

        # constants for packing
        self.low_shft = self.num_low - self.type_bits
        self.pk_mask = 2 ** self.low_shft - 1

    def unpack(self, h: int):
        """
        unpack int header into (adr, pid, is_write, is_multi, counter).
        Type field layout: [is_write:1][is_multi:1][counter:3].
        """
        low = h & self.low_mask
        high = h & self.high_mask

        adr = h >> self.num_low & self.ad_mask
        if adr == self.num_adr:  # if adr high just move to adr low
            adr = 0
        type_field = h & self.type_mask
        return (
            adr,
            ((high >> self.ad_bits) + low) >> self.type_bits,
            bool((type_field >> 4) & 0x1),
            bool((type_field >> 3) & 0x1),
            type_field & 0x7,
        )

    def pack(self, *, pid: int, adr: int, is_write: bool=False,
             is_multi: bool=False, counter: int=0) -> int:
        high = pid >> self.low_shft  # grab priority bits
        low = pid & self.pk_mask  # grab low bits
        hdr = ((((high << self.ad_bits) + adr) << self.low_shft) + low) << self.type_bits
        type_bits = (int(is_write) << 4) | (int(is_multi) << 3) | (counter & 0x07)
        hdr |= type_bits
        return hdr
