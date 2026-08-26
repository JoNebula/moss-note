# Third-party notices

This repository contains the MOSS Note application source under the MIT
License. Model weights, vLLM, CUDA libraries, and uploaded media are not part of
the repository or its source release.

## Models and inference runtime

The installation scripts download the following artifacts directly from their
official upstream repositories:

| Component | License | Upstream evidence | Included here? |
|---|---|---|---|
| MOSS-Transcribe-Diarize 0.9B | Apache-2.0 | [model card](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize), [official repository license](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/blob/main/LICENSE) | No |
| Qwen3.8-27B-FP8 | Apache-2.0 | [model card](https://huggingface.co/Qwen/Qwen3.8-27B-FP8), [model LICENSE](https://huggingface.co/Qwen/Qwen3.8-27B-FP8/blob/main/LICENSE) | No |
| vLLM | Apache-2.0 | [official license](https://github.com/vllm-project/vllm/blob/main/LICENSE) | No |

The MOSS Hugging Face repository declares `license: apache-2.0` in its model
card but does not currently include a standalone `LICENSE` file. Its linked
official GitHub source repository does contain the full Apache-2.0 license.

If you redistribute downloaded model files or vLLM rather than merely using
them, retain their license and attribution files and comply with Apache License
2.0 section 4. This project does not change the upstream licenses.

## Application dependencies

Runtime dependencies are installed from PyPI and are not vendored in this
source repository:

| Package | License |
|---|---|
| FastAPI | MIT |
| HTTPX | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| Uvicorn | BSD-3-Clause |

Their transitive dependencies retain their own licenses and metadata in the
installed Python environment.

## FFmpeg and container images

FFmpeg is invoked as a separate executable. The Dockerfile installs the FFmpeg
package supplied by the base Debian distribution. Some distribution builds are
compiled with GPL features. Building an image for your own use does not add an
FFmpeg binary to this Git repository, but anyone who publishes that resulting
binary image is responsible for satisfying the corresponding FFmpeg and
distribution source/notice obligations.

The project CI builds the image for validation but does not upload or publish
it. No prebuilt image is attached to the GitHub release.

This notice is an engineering compliance summary, not legal advice.
