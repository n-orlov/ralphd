## This is a retry: your previous attempt ran out of output budget

Your previous attempt at this judgement ran out of output budget before it
emitted a verdict, so nothing it concluded was recorded. Do not repeat that
investigation at the same depth. Keep this one to a bounded investigation:
read the PRD's requirements, check the finished work against each of them with
targeted reads and commands, and stop looking as soon as you can decide.
Reserve enough output for the verdict payload itself, and emit your verdict
payload as the last line of your reply within that bounded investigation.
