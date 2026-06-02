// Keybinds config (mod,key Hyprland style), set to empty or delete to disable auto creation
// For multiple modifiers, use + to separate them
// NOTE: On Niri, keybinds cannot be created dynamically. The app will print instructions
// to the terminal telling you what to add to ~/.config/niri/config.kdl.
export const keybinds = {
    fissure: ",F2", // Trigger manual fissure reward detection
    kiosk:   ",F4", // Trigger Ducat Kiosk scan
    gui: {
        toggle: ",F3", // Toggle gui overlay
    },
};

// Path to Warframe's EE.log
// Default assumes Warframe is installed locally via Steam/Proton.
// If playing via GeForce NOW (or any setup without a local EE.log), create a dummy
// empty file and point to it, then use manual trigger only (set autodetect to false):
//   touch ~/dummy_EE.log
//   logPath: `${Utils.HOME}/dummy_EE.log`
export const logPath = `${Utils.HOME}/.local/share/Steam/steamapps/compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log`;

// Whether to autodetect fissure reward screen via EE.log
// Set to false if using a dummy EE.log (e.g. GeForce NOW users) or if auto detection is unreliable
export const autodetect = false;

// Enable debug messages
export const debugMode = true;
