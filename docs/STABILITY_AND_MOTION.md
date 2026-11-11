# Firmware, reconnection and interaction

Firmware installation still requires the existing USB review and an explicit
start. The bootloader is excluded from the image. Two matching internal and SPI
backups precede the single flash attempt; readback verifies the selected ELF,
bootloader preservation, SPI preservation, OS version and Blue Shark capability.

## Long flasher output

The client output pipe is drained independently of the GUI. Logs retain a bounded
head and tail plus diagnostic evidence. Passing the log capacity never kills a
running client. Errors in the omitted middle remain failure evidence. A log write
failure is recorded while the pipe continues to drain. The existing operation
deadline remains in force, and a failed write never triggers another flash attempt.

The pinned client's expected transition into its bootloader may temporarily lose
the COM port. Only explicit port-open errors 2/1168/121 and the matching invalid-port
line during that transition are classified as transient. The same lines before the
transition, after writing begins, access-denied errors, write failures and a nonzero
exit remain failures. `All done` is not readback proof.

After flashing, USB enumeration must consistently identify one matching VID/PID
and serial number on a stable port. One transient appearance, duplicate devices
or another serial number cannot select the verification target.

## Idle reconnection

With **Keep connection** enabled, an unexpected idle connection loss starts a
bounded recovery cycle: at most three fresh version probes within 45 seconds.
No card read, card write, recovery attack or firmware command is replayed.

Changing connection settings, starting a manual operation, Stop, closing the app
or starting firmware installation cancels pending recovery. An active operation
or an unresolved RF cleanup retains its existing controller safeguards. USB COM
renumbering is followed automatically only when one device matches the previously
observed serial number. Blue Shark retains its explicit paired SPP port and the
existing bounded pre-READY retry policy.

Every restored connection requires a fresh OS response on the correct negotiated
transport. An old banner, a remembered COM port or a successful process exit is
insufficient.

## Buttons and scrolling

Application buttons blend their real styled backgrounds and borders on hover,
press and selection; labels and focus remain current. Activation, cancellation
and disabled states remain immediate. Animation does not resize controls or
install opacity effects on forms.

Workbench navigation, HF/LF tabs, saved-card tabs and laboratory sections use
220 ms transitions. Selection and controller callbacks change immediately. An
input-transparent picture of the outgoing page fades away above the live target;
the persistent connection strip and Stop stay outside it. Rapid navigation starts
from the currently visible frame. Completion, resize, hide and reduced motion
release the picture; no screenshots are written to disk by these effects.

Manual scrollbar interaction and native touchpad gestures interrupt pending
wheel scrolling. The interface follows Qt's system reduced-motion preference.
Tests compare actual rendered intermediate frames and exercise the production
navigation paths, rather than only checking that an animation timer is running.

## Stop and exit

Stopping a read transfers ownership of the current step to cancellation cleanup
without changing its completion token. Its matching result advances to a fresh
version check; late results from the old step cannot complete the new check.
Repeated Stop keeps the pending job intact. If no client remains, a version check
starts directly instead of waiting for a completion that cannot arrive.
A failed check retains the unconfirmed device state and permits exiting the idle
application. Active firmware installation and unsaved edits retain their guards.

## Validation boundaries

Tests use synthetic memory and ELF images, actual fake-client subprocesses, framed
connection loss/cancellation, and offscreen Qt widget events. A live `hw version`
probe is a read-only connection check. It does not validate a physical flash or
power-cycle recovery. Physical RF and firmware results must be recorded separately.

## Кратко по-русски

Большой журнал больше не прерывает прошивку. Временное исчезновение порта при
переходе в загрузчик отличается от ошибки записи; окончательный результат по-прежнему
подтверждается чтением памяти. После обрыва в простое проверяется только связь,
без повторной отправки операций с картой. Для USB смена номера COM отслеживается
по серийному номеру. Короткие анимации кнопок не задерживают действия.
