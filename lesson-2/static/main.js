function sayHello() {
    console.log("Click")
}
document.addEventListener('DOMContentLoaded', () => {
    const exitButton = document.getElementById('button1');
    
    if (exitButton) {
        exitButton.addEventListener('click', () => {
            window.location.href = '/chat_page'; 
        });
    }
});


