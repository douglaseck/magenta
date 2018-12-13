"""A module for implementing interaction between MIDI and SequenceGenerators."""

import abc
import threading
import time

import tensorflow as tf

import magenta
from magenta.protobuf import generator_pb2
from magenta.protobuf import music_pb2


class MidiInteractionException(Exception):
  """Base class for exceptions in this module."""
  pass


def adjust_sequence_times(sequence, delta_time):
  """Adjusts note and total NoteSequence times by `delta_time`."""
  retimed_sequence = music_pb2.NoteSequence()
  retimed_sequence.CopyFrom(sequence)

  for note in retimed_sequence.notes:
    note.start_time += delta_time
    note.end_time += delta_time
  retimed_sequence.total_time += delta_time
  return retimed_sequence


class MidiInteraction(threading.Thread):
  """Base class for handling interaction between MIDI and SequenceGenerator.

  Child classes will provided the "main loop" of an interactive session between
  a MidiHub used for MIDI I/O and sequences generated by a SequenceGenerator in
  their `run` methods.

  Should be started by calling `start` to launch in a separate thread.

  Args:
    midi_hub: The MidiHub to use for MIDI I/O.
    sequence_generators: A collection of SequenceGenerator objects.
    qpm: The quarters per minute to use for this interaction. May be overriden
       by control changes sent to `tempo_control_number`.
    generator_select_control_number: An optional MIDI control number whose
       value to use for selection a sequence generator from the collection.
       Must be provided if `sequence_generators` contains multiple
       SequenceGenerators.
    tempo_control_number: An optional MIDI control number whose value to use to
       determine the qpm for this interaction. On receipt of a control change,
       the qpm will be set to 60 more than the control change value.
    temperature_control_number: The optional control change number to use for
        controlling generation softmax temperature.

  Raises:
    ValueError: If `generator_select_control_number` is None and
        `sequence_generators` contains multiple SequenceGenerators.
  """
  _metaclass__ = abc.ABCMeta

  # Base QPM when set by a tempo control change.
  _BASE_QPM = 60

  def __init__(self,
               midi_hub,
               sequence_generators,
               qpm,
               generator_select_control_number=None,
               tempo_control_number=None,
               temperature_control_number=None):
    if generator_select_control_number is None and len(sequence_generators) > 1:
      raise ValueError(
          '`generator_select_control_number` cannot be None if there are '
          'multiple SequenceGenerators.')
    self._midi_hub = midi_hub
    self._sequence_generators = sequence_generators
    self._default_qpm = qpm
    self._generator_select_control_number = generator_select_control_number
    self._tempo_control_number = tempo_control_number
    self._temperature_control_number = temperature_control_number

    # A signal to tell the main loop when to stop.
    self._stop_signal = threading.Event()
    super(MidiInteraction, self).__init__()

  @property
  def _sequence_generator(self):
    """Returns the SequenceGenerator selected by the current control value."""
    if len(self._sequence_generators) == 1:
      return self._sequence_generators[0]
    val = self._midi_hub.control_value(self._generator_select_control_number)
    val = 0 if val is None else val
    return self._sequence_generators[val % len(self._sequence_generators)]

  @property
  def _qpm(self):
    """Returns the qpm based on the current tempo control value."""
    val = self._midi_hub.control_value(self._tempo_control_number)
    return self._default_qpm if val is None else val + self._BASE_QPM

  @property
  def _temperature(self, min_temp=0.1, max_temp=2.0, default=1.0):
    """Returns the temperature based on the current control value.

    Linearly interpolates between `min_temp` and `max_temp`.

    Args:
      min_temp: The minimum temperature, which will be returned when value is 0.
      max_temp: The maximum temperature, which will be returned when value is
          127.
      default: The temperature to return if control value is None.

    Returns:
      A float temperature value based on the 8-bit MIDI control value.
    """
    val = self._midi_hub.control_value(self._temperature_control_number)
    if val is None:
      return default
    return min_temp + (val / 127.) * (max_temp - min_temp)

  @abc.abstractmethod
  def run(self):
    """The main loop for the interaction.

    Must exit shortly after `self._stop_signal` is set.
    """
    pass

  def stop(self):
    """Stops the main loop, and blocks until the interaction is stopped."""
    self._stop_signal.set()
    self.join()


class CallAndResponseMidiInteraction(MidiInteraction):
  """Implementation of a MidiInteraction for interactive "call and response".

  Alternates between receiving input from the MidiHub ("call") and playing
  generated sequences ("response"). During the call stage, the input is captured
  and used to generate the response, which is then played back during the
  response stage.

  The call phrase is started when notes are received and ended by an external
  signal (`end_call_signal`) or after receiving no note events for a full tick.
  The response phrase is immediately generated and played. Its length is
  optionally determined by a control value set for
  `response_ticks_control_number` or by the length of the call.

  Args:
    midi_hub: The MidiHub to use for MIDI I/O.
    sequence_generators: A collection of SequenceGenerator objects.
    qpm: The quarters per minute to use for this interaction. May be overriden
       by control changes sent to `tempo_control_number`.
    generator_select_control_number: An optional MIDI control number whose
       value to use for selection a sequence generator from the collection.
       Must be provided if `sequence_generators` contains multiple
       SequenceGenerators.
    clock_signal: An optional midi_hub.MidiSignal to use as a clock. Each tick
        period should have the same duration. No other assumptions are made
        about the duration, but is typically equivalent to a bar length. Either
        this or `tick_duration` must be specified.be
    tick_duration: An optional float specifying the duration of a tick period in
        seconds. No assumptions are made about the duration, but is typically
        equivalent to a bar length. Either this or `clock_signal` must be
        specified.
    end_call_signal: The optional midi_hub.MidiSignal to use as a signal to stop
        the call phrase at the end of the current tick.
    panic_signal: The optional midi_hub.MidiSignal to use as a signal to end
        all open notes and clear the playback sequence.
    mutate_signal: The optional midi_hub.MidiSignal to use as a signal to
        generate a new response sequence using the current response as the
        input.
    allow_overlap: A boolean specifying whether to allow the call to overlap
        with the response.
    metronome_channel: The optional 0-based MIDI channel to output metronome on.
        Ignored if `clock_signal` is provided.
    min_listen_ticks_control_number: The optional control change number to use
        for controlling the minimum call phrase length in clock ticks.
    max_listen_ticks_control_number: The optional control change number to use
        for controlling the maximum call phrase length in clock ticks. Call
        phrases will automatically be ended and responses generated when this
        length is reached.
    response_ticks_control_number: The optional control change number to use for
        controlling the length of the response in clock ticks.
    tempo_control_number: An optional MIDI control number whose value to use to
       determine the qpm for this interaction. On receipt of a control change,
       the qpm will be set to 60 more than the control change value.
    temperature_control_number: The optional control change number to use for
        controlling generation softmax temperature.
    loop_control_number: The optional control change number to use for
        determining whether the response should be looped. Looping is enabled
        when the value is 127 and disabled otherwise.
    state_control_number: The optinal control change number to use for sending
        state update control changes. The values are 0 for `IDLE`, 1 for
        `LISTENING`, and 2 for `RESPONDING`.

    Raises:
      ValueError: If exactly one of `clock_signal` or `tick_duration` is not
         specified.
  """

  class State(object):
    """Class holding state value representations."""
    IDLE = 0
    LISTENING = 1
    RESPONDING = 2

    _STATE_NAMES = {
        IDLE: 'Idle', LISTENING: 'Listening', RESPONDING: 'Responding'}

    @classmethod
    def to_string(cls, state):
      return cls._STATE_NAMES[state]

  def __init__(self,
               midi_hub,
               sequence_generators,
               qpm,
               generator_select_control_number,
               clock_signal=None,
               tick_duration=None,
               end_call_signal=None,
               panic_signal=None,
               mutate_signal=None,
               allow_overlap=False,
               metronome_channel=None,
               min_listen_ticks_control_number=None,
               max_listen_ticks_control_number=None,
               response_ticks_control_number=None,
               tempo_control_number=None,
               temperature_control_number=None,
               loop_control_number=None,
               state_control_number=None):
    super(CallAndResponseMidiInteraction, self).__init__(
        midi_hub, sequence_generators, qpm, generator_select_control_number,
        tempo_control_number, temperature_control_number)
    if [clock_signal, tick_duration].count(None) != 1:
      raise ValueError(
          'Exactly one of `clock_signal` or `tick_duration` must be specified.')
    self._clock_signal = clock_signal
    self._tick_duration = tick_duration
    self._end_call_signal = end_call_signal
    self._panic_signal = panic_signal
    self._mutate_signal = mutate_signal
    self._allow_overlap = allow_overlap
    self._metronome_channel = metronome_channel
    self._min_listen_ticks_control_number = min_listen_ticks_control_number
    self._max_listen_ticks_control_number = max_listen_ticks_control_number
    self._response_ticks_control_number = response_ticks_control_number
    self._loop_control_number = loop_control_number
    self._state_control_number = state_control_number
    # Event for signalling when to end a call.
    self._end_call = threading.Event()
    # Event for signalling when to flush playback sequence.
    self._panic = threading.Event()
    # Even for signalling when to mutate response.
    self._mutate = threading.Event()

  def _update_state(self, state):
    """Logs and sends a control change with the state."""
    if self._state_control_number is not None:
      self._midi_hub.send_control_change(self._state_control_number, state)
    tf.logging.info('State: %s', self.State.to_string(state))

  def _end_call_callback(self, unused_captured_seq):
    """Method to use as a callback for setting the end call signal."""
    self._end_call.set()
    tf.logging.info('End call signal received.')

  def _panic_callback(self, unused_captured_seq):
    """Method to use as a callback for setting the panic signal."""
    self._panic.set()
    tf.logging.info('Panic signal received.')

  def _mutate_callback(self, unused_captured_seq):
    """Method to use as a callback for setting the mutate signal."""
    self._mutate.set()
    tf.logging.info('Mutate signal received.')

  @property
  def _min_listen_ticks(self):
    """Returns the min listen ticks based on the current control value."""
    val = self._midi_hub.control_value(
        self._min_listen_ticks_control_number)
    return 0 if val is None else val

  @property
  def _max_listen_ticks(self):
    """Returns the max listen ticks based on the current control value."""
    val = self._midi_hub.control_value(
        self._max_listen_ticks_control_number)
    return float('inf') if not val else val

  @property
  def _should_loop(self):
    return (self._loop_control_number and
            self._midi_hub.control_value(self._loop_control_number) == 127)

  def _generate(self, input_sequence, zero_time, response_start_time,
                response_end_time):
    """Generates a response sequence with the currently-selected generator.

    Args:
      input_sequence: The NoteSequence to use as a generation seed.
      zero_time: The float time in seconds to treat as the start of the input.
      response_start_time: The float time in seconds for the start of
          generation.
      response_end_time: The float time in seconds for the end of generation.

    Returns:
      The generated NoteSequence.
    """
    # Generation is simplified if we always start at 0 time.
    response_start_time -= zero_time
    response_end_time -= zero_time

    generator_options = generator_pb2.GeneratorOptions()
    generator_options.input_sections.add(
        start_time=0,
        end_time=response_start_time)
    generator_options.generate_sections.add(
        start_time=response_start_time,
        end_time=response_end_time)

    # Get current temperature setting.
    generator_options.args['temperature'].float_value = self._temperature

    # Generate response.
    tf.logging.info(
        "Generating sequence using '%s' generator.",
        self._sequence_generator.details.id)
    tf.logging.debug('Generator Details: %s',
                     self._sequence_generator.details)
    tf.logging.debug('Bundle Details: %s',
                     self._sequence_generator.bundle_details)
    tf.logging.debug('Generator Options: %s', generator_options)
    response_sequence = self._sequence_generator.generate(
        adjust_sequence_times(input_sequence, -zero_time), generator_options)
    response_sequence = magenta.music.trim_note_sequence(
        response_sequence, response_start_time, response_end_time)
    return adjust_sequence_times(response_sequence, zero_time)

  def run(self):
    """The main loop for a real-time call and response interaction."""
    start_time = time.time()
    self._captor = self._midi_hub.start_capture(self._qpm, start_time)

    if not self._clock_signal and self._metronome_channel is not None:
      self._midi_hub.start_metronome(
          self._qpm, start_time, channel=self._metronome_channel)

    # Set callback for end call signal.
    if self._end_call_signal is not None:
      self._captor.register_callback(self._end_call_callback,
                                     signal=self._end_call_signal)
    if self._panic_signal is not None:
      self._captor.register_callback(self._panic_callback,
                                     signal=self._panic_signal)
    if self._mutate_signal is not None:
      self._captor.register_callback(self._mutate_callback,
                                     signal=self._mutate_signal)

    # Keep track of the end of the previous tick time.
    last_tick_time = time.time()

    # Keep track of the duration of a listen state.
    listen_ticks = 0

    # Start with an empty response sequence.
    response_sequence = music_pb2.NoteSequence()
    response_start_time = 0
    response_duration = 0
    player = self._midi_hub.start_playback(
        response_sequence, allow_updates=True)

    # Enter loop at each clock tick.
    for captured_sequence in self._captor.iterate(signal=self._clock_signal,
                                                  period=self._tick_duration):
      if self._stop_signal.is_set():
        break
      if self._panic.is_set():
        response_sequence = music_pb2.NoteSequence()
        player.update_sequence(response_sequence)
        self._panic.clear()

      tick_time = captured_sequence.total_time

      # Set to current QPM, since it might have changed.
      if not self._clock_signal and self._metronome_channel is not None:
        self._midi_hub.start_metronome(
            self._qpm, tick_time, channel=self._metronome_channel)
      captured_sequence.tempos[0].qpm = self._qpm

      tick_duration = tick_time - last_tick_time
      last_end_time = (max(note.end_time for note in captured_sequence.notes)
                       if captured_sequence.notes else 0.0)

      # True iff there was no input captured during the last tick.
      silent_tick = last_end_time <= last_tick_time

      if not silent_tick:
        listen_ticks += 1

      if not captured_sequence.notes:
        # Reset captured sequence since we are still idling.
        if response_sequence.total_time <= tick_time:
          self._update_state(self.State.IDLE)
        if self._captor.start_time < tick_time:
          self._captor.start_time = tick_time
        self._end_call.clear()
        listen_ticks = 0
      elif (self._end_call.is_set() or
            silent_tick or
            listen_ticks >= self._max_listen_ticks):
        if listen_ticks < self._min_listen_ticks:
          tf.logging.info(
              'Input too short (%d vs %d). Skipping.',
              listen_ticks,
              self._min_listen_ticks)
          self._captor.start_time = tick_time
        else:
          # Create response and start playback.
          self._update_state(self.State.RESPONDING)

          capture_start_time = self._captor.start_time

          if silent_tick:
            # Move the sequence forward one tick in time.
            captured_sequence = adjust_sequence_times(
                captured_sequence, tick_duration)
            captured_sequence.total_time = tick_time
            capture_start_time += tick_duration

          # Compute duration of response.
          num_ticks = self._midi_hub.control_value(
              self._response_ticks_control_number)

          if num_ticks:
            response_duration = num_ticks * tick_duration
          else:
            # Use capture duration.
            response_duration = tick_time - capture_start_time

          response_start_time = tick_time
          response_sequence = self._generate(
              captured_sequence,
              capture_start_time,
              response_start_time,
              response_start_time + response_duration)

          # If it took too long to generate, push response to next tick.
          if (time.time() - response_start_time) >= tick_duration / 4:
            push_ticks = (
                (time.time() - response_start_time) // tick_duration + 1)
            response_start_time += push_ticks * tick_duration
            response_sequence = adjust_sequence_times(
                response_sequence, push_ticks * tick_duration)
            tf.logging.warn(
                'Response too late. Pushing back %d ticks.', push_ticks)

          # Start response playback. Specify the start_time to avoid stripping
          # initial events due to generation lag.
          player.update_sequence(
              response_sequence, start_time=response_start_time)

          # Optionally capture during playback.
          if self._allow_overlap:
            self._captor.start_time = response_start_time
          else:
            self._captor.start_time = response_start_time + response_duration

        # Clear end signal and reset listen_ticks.
        self._end_call.clear()
        listen_ticks = 0
      else:
        # Continue listening.
        self._update_state(self.State.LISTENING)

      # Potentially loop or mutate previous response.
      if self._mutate.is_set() and not response_sequence.notes:
        self._mutate.clear()
        tf.logging.warn('Ignoring mutate request with nothing to mutate.')

      if (response_sequence.total_time <= tick_time and
          (self._should_loop or self._mutate.is_set())):
        if self._mutate.is_set():
          new_start_time = response_start_time + response_duration
          new_end_time = new_start_time + response_duration
          response_sequence = self._generate(
              response_sequence,
              response_start_time,
              new_start_time,
              new_end_time)
          response_start_time = new_start_time
          self._mutate.clear()

        response_sequence = adjust_sequence_times(
            response_sequence, tick_time - response_start_time)
        response_start_time = tick_time
        player.update_sequence(
            response_sequence, start_time=tick_time)

      last_tick_time = tick_time

    player.stop()

  def stop(self):
    self._stop_signal.set()
    self._captor.stop()
    self._midi_hub.stop_metronome()
    super(CallAndResponseMidiInteraction, self).stop()
