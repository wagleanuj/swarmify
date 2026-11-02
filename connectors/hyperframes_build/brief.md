Build the HyperFrames project in this folder (your working directory) so that composition `$composition` turns the script at $input into a finished short video.
Read the script first. The render passes it as variables: window.__hyperframes.getVariables() returns {script: <its text>, scriptPath: <its path>}, so the composition must read the script from there, not from a copied file.
Copy what the video uses of the stage's other inputs and the world's assets, when below names any, into the project.
