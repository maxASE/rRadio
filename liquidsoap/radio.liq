# rRadio audio engine.
#
# Every channel is its own little radio: a base (a relayed radio stream, or
# music the controller pushes, e.g. from Navidrome) under a cut-in queue.
# Cut-ins pushed by the controller interrupt the base immediately and play one
# after another; when the queue is empty the base fades back in.
#
# The channel list is read from CHANNELS_FILE (written by the controller) at
# start. When channels are added or removed the controller restarts this
# process, and Docker brings it back up.
#
# The controller drives everything through a tiny HTTP API on LIQ_API_PORT.
# Per-channel calls take ?ch=<channel id>.

def env(name, default) =
  environment.get(default=default, name)
end

settings.log.stdout := true
settings.log.file := false
settings.log.level := int_of_string(env("LOG_LEVEL", "3"))
settings.harbor.bind_addrs := ["0.0.0.0"]

api_port = int_of_string(env("LIQ_API_PORT", "8005"))
ice_host = env("ICECAST_HOST", "icecast")
ice_port = int_of_string(env("ICECAST_PORT", "8000"))
ice_pass = env("ICECAST_SOURCE_PASSWORD", "hackme")
station_name = env("STATION_NAME", "rRadio")
fade_time = float_of_string(env("FADE_SECONDS", "1.5"))
cutin_gain = float_of_string(env("CUTIN_GAIN_DB", "0."))
enabled_formats = ref(string.split(separator=",", env("STREAMS", "mp3")))
channels_file = env("CHANNELS_FILE", "/data/channels.json")

default_channels = '[{"id": "main", "mount": "radio", "name": "#{station_name}"}]'

def read_channels() =
  try
    if file.exists(channels_file) then file.contents(channels_file) else default_channels end
  catch _ do
    default_channels
  end
end

let json.parse (channel_list : [{id: string, mount: string, name: string}]) =
  try
    read_channels()
  catch _ do
    default_channels
  end

if channel_list == [] then
  log.severe("No channels in #{channels_file}")
end

# Outputs. Encoder settings must be literals in Liquidsoap, so each format has
# a fixed, sensible bitrate. Every format can be switched on and off at
# runtime (the controller does this from the web UI); a switched-off format
# disconnects its Icecast mount.
def icecast_out(enc, fmt, mount, name, s) =
  gated = switch(track_sensitive=false, [({list.mem(fmt, enabled_formats())}, s)])
  output.icecast(
    enc,
    fallible=true,
    host=ice_host,
    port=ice_port,
    password=ice_pass,
    mount="/#{mount}.#{fmt}",
    name=name,
    description="Radio with automatic cut-ins",
    gated
  )
end

def req_ids(reqs) =
  list.map(fun (r) -> request.id(r), reqs)
end

def make_channel(c) =
  # Name of the station or program on the base, set by the controller.
  # It is used as the title whenever the stream itself sends none.
  base_name = ref(c.name)
  # "stream" relays base_stream; "music" plays the music queue.
  base_kind = ref("stream")

  def base_title(m) =
    t = list.assoc(default="", "title", m)
    if t == "" then
      [("title", base_name()), ("artist", c.name)]
    else
      [("title", t), ("artist", list.assoc(default="", "artist", m))]
    end
  end

  # Radio stream: the URL is set at runtime by the controller.
  base_stream = input.http(
    id="base_#{c.id}",
    max_buffer=10.,
    start=false,
    metadata_filter=base_title,
    "http://127.0.0.1:1/none"
  )
  base_stream.on_connect(synchronous=false, fun (_) -> base_stream.insert_metadata(base_title([])))

  # Music: the controller keeps this queue topped up with downloaded songs.
  music = request.queue(id="music_#{c.id}")

  base = switch(
    id="base_switch_#{c.id}",
    track_sensitive=false,
    [({base_kind() == "music"}, music), ({base_kind() == "stream"}, base_stream)]
  )

  # Cut-in queue: every pushed request plays in order.
  cutin = request.queue(id="cutin_#{c.id}")
  cutin_out = amplify(lin_of_dB(cutin_gain), cutin)

  def to_cutin(old, new) =
    sequence([fade.out(duration=fade_time, old), fade.in(duration=fade_time, new)])
  end

  # Show the base's title again when it comes back after a cut-in.
  def to_base(old, new) =
    if base_kind() == "music" then
      music.insert_metadata(music.last_metadata() ?? [])
    else
      base_stream.insert_metadata(base_title(base_stream.last_metadata() ?? []))
    end
    sequence([fade.out(duration=fade_time, old), fade.in(duration=fade_time, new)])
  end

  radio = fallback(
    id="radio_#{c.id}",
    track_sensitive=false,
    transition_length=fade_time,
    transitions=[to_cutin, to_base],
    [cutin_out, base]
  )
  radio = mksafe(radio)

  # Now-playing metadata, kept in memory only.
  current_meta = ref([])
  current_since = ref(time())
  radio.on_metadata(synchronous=false, fun (m) -> begin
    current_meta := m
    current_since := time()
  end)

  def meta_value(key) =
    list.assoc(default="", key, current_meta())
  end

  icecast_out(%mp3(bitrate = 128, samplerate = 44100), "mp3", c.mount, c.name, radio)
  icecast_out(%fdkaac(bitrate = 96, aot = "mpeg4_he_aac"), "aac", c.mount, c.name, radio)
  icecast_out(%ogg(%opus(bitrate = 96)), "opus", c.mount, c.name, radio)
  # Data saver (HE-AAC suits low bitrates) and best (plain AAC suits high ones).
  icecast_out(%fdkaac(bitrate = 48, aot = "mpeg4_he_aac"), "aac48", c.mount, c.name, radio)
  icecast_out(%fdkaac(bitrate = 192, aot = "mpeg4_aac_lc"), "aac192", c.mount, c.name, radio)

  def status() =
    cur = cutin.current()
    mcur = music.current()
    {
      id = c.id,
      mount = c.mount,
      name = c.name,
      mode = if cutin.is_ready() then "cutin" else "base" end,
      base_kind = base_kind(),
      base_url = base_stream.url(),
      base_up = if base_kind() == "music" then music.is_ready() else base_stream.is_ready() end,
      cutin_current = if null.defined(cur) then request.id(null.get(cur)) else -1 end,
      cutin_queue = req_ids(cutin.queue()),
      cutin_elapsed = cutin.elapsed(),
      cutin_remaining = cutin.remaining(),
      music_current = if null.defined(mcur) then request.id(null.get(mcur)) else -1 end,
      music_queue = req_ids(music.queue()),
      title = meta_value("title"),
      artist = meta_value("artist"),
      since = current_since()
    }
  end

  def push(uri) =
    r = request.create(uri)
    cutin.push(r)
    request.id(r)
  end

  def push_music(uri) =
    r = request.create(uri)
    music.push(r)
    request.id(r)
  end

  # Drop queued songs and end the current one, e.g. for another playlist.
  def flush_music() =
    music.set_queue([])
    music.skip()
  end

  def set_base(kind, new_url, name) =
    base_name := name
    if kind == "music" then
      if base_kind() != "music" then
        log.important("#{c.id}: switching base to music")
        base_kind := "music"
        base_stream.stop()
      end
    else
      if new_url != base_stream.url() or base_kind() != "stream" or not base_stream.is_started() then
        log.important("#{c.id}: switching base stream to #{new_url}")
        base_kind := "stream"
        base_stream.set_url({new_url})
        base_stream.stop()
        base_stream.start()
      end
    end
  end

  {
    id = c.id,
    status = status,
    push = push,
    push_music = push_music,
    flush_music = flush_music,
    skip = fun () -> cutin.skip(),
    skip_music = fun () -> music.skip(),
    set_base = set_base
  }
end

channels = list.map(make_channel, channel_list)

# Run `f` on the channel named in ?ch=, or answer 404.
def with_channel(req, res, f) =
  ch = list.assoc(default="", "ch", req.query)
  found = list.filter(fun (c) -> c.id == ch, channels)
  if not list.is_empty(found) then
    f(list.hd(found))
  else
    res.status_code(404)
    res.json({error = "unknown channel"})
  end
end

harbor.http.register(port=api_port, method="GET", "/status", fun (_, res) -> begin
  res.json({
    streams = enabled_formats(),
    channels = list.map(fun (c) -> c.status(), channels)
  })
end)

# Body: a request URI, optionally prefixed with annotate:key="value",...:
harbor.http.register(port=api_port, method="POST", "/push", fun (req, res) ->
  with_channel(req, res, fun (c) -> res.json({rid = c.push(string.trim(req.body()))})))

harbor.http.register(port=api_port, method="POST", "/skip", fun (req, res) ->
  with_channel(req, res, fun (c) -> begin c.skip() res.json({ok = true}) end))

harbor.http.register(port=api_port, method="POST", "/music/push", fun (req, res) ->
  with_channel(req, res, fun (c) -> res.json({rid = c.push_music(string.trim(req.body()))})))

harbor.http.register(port=api_port, method="POST", "/music/flush", fun (req, res) ->
  with_channel(req, res, fun (c) -> begin c.flush_music() res.json({ok = true}) end))

harbor.http.register(port=api_port, method="POST", "/music/skip", fun (req, res) ->
  with_channel(req, res, fun (c) -> begin c.skip_music() res.json({ok = true}) end))

# Body: the stream URL (ignored for kind=music); ?kind=stream|music&name=...
harbor.http.register(port=api_port, method="POST", "/base", fun (req, res) ->
  with_channel(req, res, fun (c) -> begin
    kind = list.assoc(default="stream", "kind", req.query)
    name = list.assoc(default=station_name, "name", req.query)
    c.set_base(kind, string.trim(req.body()), name)
    res.json({ok = true})
  end))

# Body: comma-separated formats to stream, e.g. "mp3,aac".
harbor.http.register(port=api_port, method="POST", "/streams", fun (req, res) -> begin
  enabled_formats := string.split(separator=",", string.trim(req.body()))
  res.json({ok = true})
end)

# Restart to pick up a changed channel list (Docker restarts the container).
harbor.http.register(port=api_port, method="POST", "/restart", fun (_, res) -> begin
  res.json({ok = true})
  thread.run(delay=0.5, shutdown)
end)
