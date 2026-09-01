;;; modelmux.el --- Run local AI models from Emacs -*- lexical-binding: t; -*-

;; Version: 0.1.0
;; Package-Requires: ((emacs "27.1"))
;; Keywords: processes, multimedia, tools

;;; Commentary:

;; A deliberately thin frontend for the ModelMux command-line interface.

;;; Code:

(require 'json)
(require 'subr-x)

(defgroup modelmux nil
  "Run local AI models through ModelMux."
  :group 'external)

(defcustom modelmux-command '("modelmux")
  "Command and leading arguments used to invoke ModelMux."
  :type '(repeat string))

(defcustom modelmux-tts-profile nil
  "TTS profile name, or nil to use the ModelMux default."
  :type '(choice (const :tag "Default" nil) string))

(defvar modelmux--process nil)
(defvar modelmux--progress-buffer " *modelmux-progress*")

(defun modelmux-stop ()
  "Stop the current ModelMux process."
  (interactive)
  (when (process-live-p modelmux--process)
    (delete-process modelmux--process)
    (message "ModelMux stopped")))

(defun modelmux--result-output (buffer)
  (with-current-buffer buffer
    (goto-char (point-min))
    (let ((result (json-parse-buffer :object-type 'alist)))
      (alist-get 'output result))))

(defun modelmux--run (task input &optional profile callback)
  (modelmux-stop)
  (let* ((output-buffer (generate-new-buffer " *modelmux-output*"))
         (progress-buffer (get-buffer-create modelmux--progress-buffer))
         (arguments (append modelmux-command
                            (list task "-" "--json" "--json-events")
                            (when profile (list "--profile" profile)))))
    (with-current-buffer progress-buffer (erase-buffer))
    (setq modelmux--process
          (make-process
           :name "modelmux"
           :command arguments
           :buffer output-buffer
           :stderr progress-buffer
           :connection-type 'pipe
           :noquery t
           :sentinel
           (lambda (process _event)
             (when (memq (process-status process) '(exit signal))
               (unwind-protect
                   (if (= (process-exit-status process) 0)
                       (let ((output (modelmux--result-output output-buffer)))
                         (message "ModelMux finished: %s" output)
                         (when callback (funcall callback output)))
                     (message "ModelMux failed: %s"
                              (string-trim
                               (with-current-buffer progress-buffer
                                 (buffer-string)))))
                 (kill-buffer output-buffer))))))
    (process-send-string modelmux--process input)
    (process-send-eof modelmux--process)
    (message "ModelMux started…")))

(defun modelmux-tts-region (start end)
  "Read the region between START and END using ModelMux."
  (interactive "r")
  (unless (use-region-p)
    (user-error "No active region"))
  (modelmux--run "tts" (buffer-substring-no-properties start end)
                 modelmux-tts-profile #'play-sound-file))

(defun modelmux-tts-buffer ()
  "Read the current buffer using ModelMux."
  (interactive)
  (modelmux--run "tts" (buffer-substring-no-properties (point-min) (point-max))
                 modelmux-tts-profile #'play-sound-file))

(provide 'modelmux)

;;; modelmux.el ends here
