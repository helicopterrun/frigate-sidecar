"""Face-training-image quality curation (B1).

Scores Frigate's auto-saved face crops (sharpness x area), records them in the
sidecar's `face_attempts` table, auto-promotes high-quality recognized crops
into the Face Library, and routes the rest to a manual review queue.
"""
