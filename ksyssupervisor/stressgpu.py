"""The GPU half of the stress test, which runs in its own process.

Kept apart from stress.py so that module stays free of Qt, and because this is
the only part that needs a graphics context. It is imported by the child
process, never by the window.

The load is a fragment shader doing deliberately expensive arithmetic over an
offscreen buffer, rather than a compute shader. That is a portability decision
first - PyQt6 exposes OpenGL function sets only up to 4.1 core, so
glDispatchCompute is not reachable from Python at all - but it is also the
wider net: fragment shading works on anything from roughly 2012 onward across
AMD, NVIDIA and Intel, while compute shaders would have excluded older parts
for no gain in heat.
"""

from __future__ import annotations

import os
import time

#: Requested first; anything modern gives at least this.
PREFERRED_VERSION = (4, 1)

#: One draw covers this many pixels. Big enough that a single frame is real
#: work, small enough that a frame still finishes promptly at low duty.
TARGET_SIZE = 1024

#: Arithmetic per pixel. Tuned so one frame is a few milliseconds on a mid
#: card - long enough to be efficient, short enough to duty-cycle smoothly.
INNER_ITERATIONS = 220

#: Graphics memory is claimed in chunks so a request larger than the card can
#: hold fills what it can instead of failing outright.
VRAM_CHUNK = 128 * 1024 * 1024

SLICE_SECONDS = 0.05

#: How long one batch of frames should take. Short enough that a stop is
#: noticed promptly, long enough that the single wait per batch is not most of
#: the cost.
#:
#: Measured at 50% requested on a Radeon RX 6800, sampling gpu_busy_percent:
#: 15 ms averaged 57% but swung between 28 and 82; 5 ms averaged 56% within a
#: 45-66 band. The average barely moved, but the reading became steady enough
#: to be worth showing, so the finer batch wins.
BATCH_SECONDS = 0.005

VERTEX_SRC = """#version 410 core
const vec2 verts[3] = vec2[3](
    vec2(-1.0, -1.0), vec2(3.0, -1.0), vec2(-1.0, 3.0));
void main() { gl_Position = vec4(verts[gl_VertexID], 0.0, 1.0); }
"""

# Every shader below is written to defeat the optimiser rather than to compute
# anything: the result feeds the output colour, so the driver cannot discard
# the loop, and each one is seeded from a uniform so it cannot be folded to a
# constant either. What differs between them is which part of the card ends up
# being the limit, which is the whole point of offering a choice.

#: Serial transcendental chain. Each line consumes the previous result, so the
#: card cannot overlap them and is limited by latency through the
#: special-function units rather than by throughput. The steadiest of the four,
#: and what this test ran before the modes existed.
TRANSCENDENTAL_SRC = """#version 410 core
uniform int iterations;
uniform float seed;
out vec4 fragColour;
void main() {
    vec4 acc = vec4(gl_FragCoord.xy * 0.001, seed, 1.0);
    for (int i = 0; i < iterations; ++i) {
        acc.x = sin(acc.y * 1.7 + seed) + cos(acc.z * 0.9);
        acc.y = sqrt(abs(acc.z * 1.3 - acc.x)) + fract(acc.w * 3.1);
        acc.z = acc.x * acc.y + sin(acc.w);
        acc.w = cos(acc.x - acc.z) * 1.0001;
    }
    fragColour = clamp(acc, 0.0, 1.0);
}
"""

#: Four independent multiply-add streams. The independence is the load: with no
#: stream waiting on another the scheduler can keep every lane busy, so this is
#: bounded by arithmetic throughput and draws more power than the chain above.
#: Each accumulator is seeded differently so none of them can be shown to be a
#: copy of another and shared.
FMA_SRC = """#version 410 core
uniform int iterations;
uniform float seed;
out vec4 fragColour;
void main() {
    vec2 uv = gl_FragCoord.xy * 0.001;
    vec4 a = vec4(uv, seed, 1.0);
    vec4 b = vec4(uv.yx, seed * 1.3 + 0.1, 0.7);
    vec4 c = vec4(uv * 1.7, seed * 0.6 + 0.2, 0.3);
    vec4 d = vec4(uv.yx * 0.4, seed * 2.1 + 0.3, 0.9);
    vec4 k = vec4(1.0000041, 0.9999967, 1.0000019, 0.9999989);
    vec4 t = vec4(seed, 0.25, 0.5, 0.75);
    for (int i = 0; i < iterations; ++i) {
        a = a * k + t;
        b = b * k + t;
        c = c * k + t;
        d = d * k + t;
        // Kept in range without a branch and without breaking the chains:
        // left to grow, these reach infinity and the hardware stops doing
        // real work on them.
        a = fract(a);
        b = fract(b);
        c = fract(c);
        d = fract(d);
    }
    fragColour = clamp((a + b + c + d) * 0.25, 0.0, 1.0);
}
"""

#: Scattered reads from a large texture. The step between samples is deliberately
#: not a small stride: consecutive iterations land far apart and wrap around the
#: texture, so the cache rarely helps and the card waits on memory instead of
#: calculating. The arithmetic between fetches is only enough to work out the
#: next coordinate, which keeps memory the limit rather than the ALUs.
MEMORY_SRC = """#version 410 core
uniform int iterations;
uniform float seed;
uniform sampler2D field;
out vec4 fragColour;
void main() {
    vec2 uv = gl_FragCoord.xy * 0.001 + seed;
    vec4 acc = vec4(0.0);
    for (int i = 0; i < iterations; ++i) {
        // A large, irrational-ish step so successive samples do not share a
        // cache line and the walk does not fall into a short cycle.
        uv = fract(uv + vec2(0.6180339887, 0.4142135624) * float(i + 1));
        // Level 0 explicitly: texture() picks a level from the screen-space
        // derivatives, and a divergent walk makes those large enough that the
        // card would fetch from a smaller mip and move far less memory.
        acc += textureLod(field, uv, 0.0);
    }
    fragColour = clamp(acc * 0.01, 0.0, 1.0);
}
"""

#: Keyed by the mode strings in stress.GPU_MODES. "combined" has no source of
#: its own - it alternates between two of these at run time - so the table that
#: pairs the two halves has to allow for that; a test checks they agree.
FRAGMENT_SOURCES = {
    "transcendental": TRANSCENDENTAL_SRC,
    "fma": FMA_SRC,
    "memory": MEMORY_SRC,
}

#: What "combined" alternates between, in order.
COMBINED_MODES = ("fma", "memory")

#: The mode used when asked for one we do not have. A stale setting should run
#: something rather than fail the test.
FALLBACK_MODE = "transcendental"

#: Side of the texture the memory workload walks. Large enough that it cannot
#: sit in cache on any current card - 2048 x 2048 RGBA is 16 MB, comfortably
#: past the last-level cache of the parts this runs on - and small enough that
#: it is a rounding error against the graphics memory the user may also ask to
#: fill.
FIELD_SIZE = 2048

#: Per-pixel iterations for the memory workload. Far lower than
#: INNER_ITERATIONS because each iteration is a texture fetch rather than a few
#: arithmetic operations, and a frame that takes too long is a frame the duty
#: cycle cannot steer.
MEMORY_ITERATIONS = 48

#: Per-pixel iterations by workload. Anything not listed uses INNER_ITERATIONS.
ITERATIONS = {"memory": MEMORY_ITERATIONS}


def workload_names(mode):
    """The shader workloads one mode runs, in the order it alternates them.

    An unknown mode falls back rather than raising: a setting saved by an older
    version, or one this build no longer has, should still put load on the card
    instead of failing the test.
    """
    if mode == "combined":
        return list(COMBINED_MODES)
    if mode in FRAGMENT_SOURCES:
        return [mode]
    return [FALLBACK_MODE]


def gpu_worker(percent, vram_bytes, stop, state, mode=FALLBACK_MODE):
    """Load the GPU until told to stop.

    `state` is a shared array the parent reads: [started, vram_mb, failed].
    Reported rather than assumed because a driver may place a buffer in system
    memory instead of graphics memory, and a stress test that quietly filled
    the wrong one would be worse than useless.

    `mode` is one of the keys in stress.GPU_MODES. It has a default so that a
    caller written against the single-workload version still runs.
    """
    try:
        _run(percent, vram_bytes, stop, state, mode)
    except Exception:
        state[2] = 1
        # The parent surfaces this; a traceback from a child that the user
        # never sees would just be noise in the journal.
        import traceback
        traceback.print_exc()


def _run(percent, vram_bytes, stop, state, mode=FALLBACK_MODE):
    # Imported here, inside the child, so the parent never loads Qt's GL stack.
    from PyQt6.QtGui import (QImage, QOffscreenSurface, QOpenGLContext,
                             QSurfaceFormat)
    from PyQt6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                                QOpenGLShader, QOpenGLShaderProgram,
                                QOpenGLTexture,
                                QOpenGLVersionFunctionsFactory,
                                QOpenGLVersionProfile,
                                QOpenGLVertexArrayObject)
    from PyQt6.QtWidgets import QApplication

    _mark_as_oom_victim()

    # A GUI application object, not QCoreApplication: creating a context needs
    # the platform plugin. Deliberately *not* forced to the "offscreen"
    # platform - that plugin provides no accelerated context, so the load would
    # land on the CPU while appearing to stress the GPU. The QOffscreenSurface
    # below is what keeps this off the user's display.
    app = QApplication([])                                  # noqa: F841

    fmt = QSurfaceFormat()
    fmt.setVersion(*PREFERRED_VERSION)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.SingleBuffer)

    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    if not surface.isValid():
        raise RuntimeError("could not create an offscreen surface")

    context = QOpenGLContext()
    context.setFormat(fmt)
    if not context.create() or not context.makeCurrent(surface):
        raise RuntimeError("could not create an OpenGL context")

    profile = QOpenGLVersionProfile()
    profile.setVersion(*PREFERRED_VERSION)
    profile.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    functions = QOpenGLVersionFunctionsFactory.get(profile, context)
    if functions is None:
        raise RuntimeError("this driver does not offer OpenGL %d.%d core"
                           % PREFERRED_VERSION)
    functions.initializeOpenGLFunctions()

    # Core profile draws nothing without a bound vertex array, even when the
    # vertices come from the shader itself.
    vao = QOpenGLVertexArrayObject()
    vao.create()
    vao.bind()

    names = workload_names(mode)

    def compile_program(name):
        program = QOpenGLShaderProgram()
        if not program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Vertex, VERTEX_SRC):
            raise RuntimeError("vertex shader: %s" % program.log())
        if not program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT_SOURCES[name]):
            raise RuntimeError("%s fragment shader: %s" % (name, program.log()))
        if not program.link():
            raise RuntimeError("%s shader link failed: %s"
                               % (name, program.log()))
        return program

    stages = []
    for name in names:
        program = compile_program(name)
        program.bind()
        field_at = program.uniformLocation("field")
        if field_at != -1:
            program.setUniformValue(field_at, 0)     # texture unit 0
        stages.append({
            "name": name,
            "program": program,
            "iterations_at": program.uniformLocation("iterations"),
            "seed_at": program.uniformLocation("seed"),
            "iterations": ITERATIONS.get(name, INNER_ITERATIONS),
            "batch": 1,
        })

    # After the shaders, so a workload that will not compile fails before
    # anything has been claimed, and before the sampler below needs binding.
    field = None
    if "memory" in names:
        field = _make_field(QOpenGLTexture, QImage)

    target = QOpenGLFramebufferObject(TARGET_SIZE, TARGET_SIZE)
    if not target.isValid():
        raise RuntimeError("could not create a render target")

    buffers = _claim_vram(QOpenGLBuffer, vram_bytes, state)
    state[0] = 1

    duty = max(0.0, min(100, percent)) / 100.0

    target.bind()
    functions.glViewport(0, 0, TARGET_SIZE, TARGET_SIZE)
    if field is not None:
        field.bind(0)

    def draw(count, stage):
        """Queue `count` frames of one workload, then wait for them all."""
        program = stage["program"]
        program.bind()
        for _ in range(count):
            program.setUniformValue(stage["iterations_at"], stage["iterations"])
            program.setUniformValue(stage["seed_at"],
                                    float((draw.frame % 360) * 0.01))
            functions.glDrawArrays(0x0004, 0, 3)        # GL_TRIANGLES
            draw.frame += 1
        # One wait per batch, not per frame. Finishing after every frame leaves
        # the card idle while Python prepares the next one, which on a fast GPU
        # is most of the time - the reason an early version of this reached
        # only a third of the load it was asked for.
        functions.glFinish()
    draw.frame = 0

    # Sized per workload rather than once for the run: a memory batch and an
    # arithmetic batch cost very different amounts, and a single shared number
    # would spend its time chasing whichever ran last.
    for stage in stages:
        stage["batch"] = _calibrate(lambda n, s=stage: draw(n, s), duty)

    turn = 0
    try:
        while not stop.is_set():
            if duty <= 0:
                time.sleep(SLICE_SECONDS)
                continue

            # With one workload this always picks the same stage; with
            # "combined" it alternates, so a batch of each lands within a few
            # milliseconds of the other and the card sees a genuinely mixed
            # load rather than two tests in sequence.
            stage = stages[turn % len(stages)]
            turn += 1

            start = time.perf_counter()
            draw(stage["batch"], stage)
            spent = time.perf_counter() - start

            # The duty comes from the ratio of work to rest, not from sizing
            # the batch to a fixed window. Sizing was tried first and was not
            # stable: the batch chased a moving target, overshot the slice, and
            # a 60% request ran at 91%. Measuring what the batch actually cost
            # and resting in proportion is exact whatever the card's speed, and
            # needs no per-vendor tuning.
            if duty < 1.0 and spent > 0:
                time.sleep(spent * (1.0 - duty) / duty)

            # The batch is only sized for responsiveness now - long enough to
            # amortise the per-batch wait, short enough to notice a stop
            # promptly - so this correction is gentle and rarely matters.
            if spent > 0:
                stage["batch"] = _clamp_batch(
                    stage["batch"] * _damped(BATCH_SECONDS / spent))
    finally:
        # Explicit, though the process exiting is what actually guarantees the
        # graphics memory comes back - measured, not assumed.
        for buf in buffers:
            try:
                buf.destroy()
            except Exception:
                pass
        if field is not None:
            try:
                field.destroy()
            except Exception:
                pass
        vao.release()
        context.doneCurrent()


def _make_field(texture_class, image_class):
    """The texture the memory workload walks.

    Filled with random bytes rather than anything structured, and sampled with
    nearest filtering and no mipmaps. All three matter: cards compress texture
    memory, and a smooth or repetitive image would compress well enough that
    the card moves far less of it than the size suggests, which would make a
    bandwidth test measure the compressor instead.
    """
    data = os.urandom(FIELD_SIZE * FIELD_SIZE * 4)
    image = image_class(data, FIELD_SIZE, FIELD_SIZE,
                        image_class.Format.Format_RGBA8888)
    if image.isNull():
        raise RuntimeError("could not build the memory workload's texture")

    texture = texture_class(
        image, texture_class.MipMapGeneration.DontGenerateMipMaps)
    texture.setMinificationFilter(texture_class.Filter.Nearest)
    texture.setMagnificationFilter(texture_class.Filter.Nearest)
    texture.setWrapMode(texture_class.WrapMode.Repeat)
    # The QImage does not own the bytes it was handed, and the texture does not
    # own the QImage; both are kept alive here until upload is certainly done.
    texture._field_source = (data, image)
    return texture


def _calibrate(draw, duty):
    """A first guess at how many frames fill the busy part of one slice.

    Measured rather than fixed, because the range of hardware this has to cover
    is enormous: the same batch size that keeps a discrete card busy for 30 ms
    would take an integrated one most of a second, turning a 40% request into a
    pinned GPU and a frozen desktop.

    The first batch is thrown away. It carries shader compilation and pipeline
    setup, which on this machine made a frame look an order of magnitude more
    expensive than it is - and a batch sized from that number reached a fifth
    of the load it was asked for.
    """
    if duty <= 0:
        return 0

    draw(4)                                     # warm up, timing discarded

    probe = 8
    start = time.perf_counter()
    draw(probe)
    per_frame = (time.perf_counter() - start) / probe
    if per_frame <= 0:
        return 32
    return _clamp_batch(BATCH_SECONDS / per_frame)


def _damped(ratio):
    """Move part of the way, not all of it: a full correction every slice
    oscillates instead of settling."""
    return max(0.5, min(2.0, 1.0 + (ratio - 1.0) * 0.3))


def _clamp_batch(frames):
    """At least one frame, or a low setting on a slow card draws nothing;
    capped so a fast card cannot queue an unresponsive amount."""
    return max(1, min(int(frames), 8192))


def _claim_vram(buffer_class, wanted, state):
    """Fill graphics memory in chunks, reporting how much was taken."""
    buffers = []
    if wanted <= 0:
        return buffers

    claimed = 0
    while claimed < wanted:
        size = min(VRAM_CHUNK, wanted - claimed)
        buf = buffer_class(buffer_class.Type.VertexBuffer)
        if not buf.create():
            break
        buf.bind()
        try:
            buf.allocate(size)
        except Exception:
            buf.destroy()
            break
        buffers.append(buf)
        claimed += size
        state[1] = claimed // (1024 * 1024)
    return buffers


def _mark_as_oom_victim():
    try:
        with open("/proc/self/oom_score_adj", "w") as handle:
            handle.write("1000")
    except OSError:
        pass
