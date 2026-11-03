# Mario Kart World 1.2.0 (v917504), main.
# The host has no Nintendo camera / GSV service. MKW calls its camera setup
# during startup even when camera sharing is unused. Skip that optional setup
# at its initialized-flag gate; the branch lands on the function's success
# epilogue and makes every later call take the same successful fast path.
CUTS = {
    'd00e4f399e411d0613aa8d26b1c02fef': [
        (0xcdebe4, '280c0037', '61000014',
         'camera context requires unavailable console GSV service; skip optional camera setup'),
        (0xcdc5a8, '080040f9', 'e00313aa',
         'MKW display event: pass its SystemEvent to the host VI vsync hook'),
        (0xcdc5ac, '085540f9', 'e1031faa',
         'MKW display event: omit the unavailable console Display object'),
        (0xcdc5b0, '00013fd6', 'a0507b94',
         'MKW display event: bind it to the host 60 Hz vsync event'),
        (0xcdc5bc, 'e10b40b9', '05000014',
         'skip attaching the console readable handle; the host VI hook owns this event'),
        (0x25a6ac, '080040f9086d40f900013fd6', '80b881521f2003d51f2003d5',
         'audio settings service is unavailable; return the SDK-recognized optional-service result 0xdc4'),
    ],
}
